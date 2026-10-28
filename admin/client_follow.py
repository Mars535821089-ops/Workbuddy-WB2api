"""Keep this machine's existing conversations when injecting another account.

Run only while the desktop client is stopped. Change the account scope of local
session rows, preserving their IDs, body paths and all other columns. Do not
clone messages, resurrect deleted rows or change ownership of cloud tasks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time


class FollowError(ValueError):
    pass


def history_db_path() -> Path:
    return Path(os.path.expandvars(os.path.expanduser(
        os.getenv("ADMIN_CLIENT_HISTORY_DB", "~/.workbuddy/workbuddy.db"))))


def _uid(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise FollowError("客户端账号 UID 无效，本次未切换")
    return value


def current_uid(auth_file: str | Path) -> str | None:
    try:
        value = json.loads(Path(auth_file).read_text(encoding="utf-8-sig"))
        uid = (value.get("account") or {}).get("uid")
        return _uid(uid) if uid else None
    except FileNotFoundError:
        return None
    except (ValueError, TypeError, AttributeError):
        raise FollowError("当前客户端账号信息无法识别，本次未切换") from None


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        raise FollowError("客户端会话界面状态损坏，本次未切换") from None
    if not isinstance(value, dict):
        raise FollowError("客户端会话界面状态格式无效，本次未切换")
    return value


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, staged = tempfile.mkstemp(prefix=".wb2api-follow-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


def _connect(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)


def rollback(backup: str, db_path: Path) -> None:
    folder = Path(backup)
    root = db_path.parent.resolve()
    manifest = _read(folder / "files.json")
    with sqlite3.connect((folder / "workbuddy.db").resolve().as_uri() + "?mode=ro&immutable=1",
                         uri=True) as source, _connect(db_path) as target:
        source.backup(target)
    for entry in manifest.get("files", []):
        target = (root / entry["relative"]).resolve()
        if not target.is_relative_to(root):
            raise FollowError("会话回滚路径无效")
        if entry["exists"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(folder / entry["copy"], target)
        else:
            target.unlink(missing_ok=True)
    finish_switch(db_path)


def _pending_path(db_path: Path) -> Path:
    return db_path.parent / "wb2api-session-follow" / "pending.json"


def finish_switch(db_path: Path) -> None:
    _pending_path(db_path).unlink(missing_ok=True)


def recover_pending(db_path: Path) -> str | None:
    """Resolve a process interruption by checking the atomically replaced login."""
    pending = _read(_pending_path(db_path))
    if not pending:
        return None
    uid = current_uid(pending["auth_file"])
    if uid == pending["target_uid"]:
        finish_switch(db_path)
        return "committed"
    if uid != pending.get("source_uid"):
        raise FollowError("上次切换中断后账号已变化，请先恢复会话备份，客户端未启动")
    rollback(pending["backup"], db_path)
    return "rolled_back"


def follow_sessions(db_path: Path, target_uid: str, source_uid: str | None,
                    auth_file: str | Path | None = None) -> dict:
    """Move visibility of all existing local conversations; never create rows."""
    _uid(target_uid)
    if source_uid:
        _uid(source_uid)
    if not db_path.is_file():
        raise FollowError("未找到本机客户端会话数据库，本次未切换")
    recover_pending(db_path)
    root = db_path.parent.resolve()
    backup_root = root / "wb2api-session-follow" / "backups"
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(prefix="switch-", dir=backup_root))
    changes: dict[Path, dict] = {}
    try:
        with _connect(db_path) as db:
            db.row_factory = sqlite3.Row
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            if not {"id", "user_id", "transport", "status", "deleted_at", "cwd"} <= columns:
                raise FollowError("客户端会话数据库版本不兼容，本次未切换")
            rows = [dict(row) for row in db.execute("SELECT * FROM sessions WHERE transport='local'")]
            owners = {row["user_id"] for row in rows if row["user_id"]}
            for owner in owners:
                _uid(owner)
            owners.add(target_uid)
            if source_uid:
                owners.add(source_uid)
            order = [source_uid] if source_uid else []
            order += [uid for uid in sorted(owners) if uid != source_uid]
            active = {row["id"]: row for row in rows
                      if row["deleted_at"] in (None, -1) and row["status"] != "archived"}
            owner_by_id = {row["id"]: row["user_id"] for row in rows}
            pins, pinned_ids = [], set()
            for owner in order:
                path = root / f"storage/user-{owner}-personal/global/conversations.json"
                for item in _read(path).get("pinned", []):
                    if not isinstance(item, dict):
                        continue
                    id_ = item.get("id")
                    if (id_ in active and id_ not in pinned_ids
                            and owner_by_id.get(id_) == owner):
                        pins.append(item)
                        pinned_ids.add(id_)
            pin_path = root / f"storage/user-{target_uid}-personal/global/conversations.json"
            pin_data = _read(pin_path)
            pin_data["pinned"] = pins
            changes[pin_path] = pin_data
            items = []
            for row in sorted(active.values(), key=lambda r: r.get("last_activity_at") or r.get("created_at") or 0,
                              reverse=True):
                cwd = row["cwd"] or ""
                item = {
                    "id": row["id"], "title": row.get("custom_title") or row.get("title") or "",
                    "transport": "local", "kind": "task",
                    "state": {"completed": "idle", "pending": "pending"}.get(row["status"], row["status"]),
                    "conversationOrigin": "local", "isPinned": row["id"] in pinned_ids,
                    "lastActivityAt": row.get("last_activity_at") or row.get("created_at") or 0,
                    "updatedAt": row.get("updated_at") or 0,
                    "space": {"type": "local" if row.get("is_playground") else "workdir",
                              "name": cwd, "cwd": cwd, "autoGenerated": bool(row.get("is_playground"))},
                }
                if row.get("project_id"):
                    item["projectId"] = row["project_id"]
                    item["space"]["type"] = "project"
                items.append(item)
            changes[root / target_uid / "sidebar-list-snapshot.json"] = {
                "version": 3, "savedAt": int(time.time() * 1000), "items": items}
            if source_uid and source_uid != target_uid:
                route = _read(root / source_uid / "startup-route.json")
                if route:
                    changes[root / target_uid / "startup-route.json"] = route
                # Only conversation-specific state follows; account/product,
                # provider, proxy and authentication configuration are excluded.
                scoped = root / f"storage/user-{source_uid}-personal/scoped"
                for source in scoped.glob("*/*.json"):
                    if source.name not in {"conversationContextUsage.json", "conversationList.json",
                                           "dismissed-history-error.json"}:
                        continue
                    relative = source.relative_to(scoped)
                    dest = root / f"storage/user-{target_uid}-personal/scoped" / relative
                    value = _read(dest)
                    value.update(_read(source))
                    changes[dest] = value

            file_backups = []
            for index, path in enumerate(changes):
                if not path.resolve().is_relative_to(root):
                    raise FollowError("客户端会话状态路径无效，本次未切换")
                entry = {"relative": str(path.relative_to(root)), "exists": path.exists(),
                         "copy": f"state-{index}.json"}
                if entry["exists"]:
                    shutil.copy2(path, backup / entry["copy"])
                    (backup / entry["copy"]).chmod(0o600)
                file_backups.append(entry)
            _write(backup / "files.json", {"files": file_backups})
            with sqlite3.connect(backup / "workbuddy.db") as copy:
                db.backup(copy)
                copy.execute("PRAGMA journal_mode=DELETE")
            (backup / "workbuddy.db").chmod(0o600)
            if auth_file is not None:
                _write(_pending_path(db_path), {
                    "backup": str(backup), "auth_file": str(Path(auth_file).resolve()),
                    "source_uid": source_uid, "target_uid": target_uid})
            try:
                for path, value in changes.items():
                    _write(path, value)
                with db:
                    rebound = db.execute(
                        "UPDATE sessions SET user_id=? WHERE transport='local' AND user_id IS NOT ?",
                        (target_uid, target_uid)).rowcount
            except Exception:
                rollback(str(backup), db_path)
                raise
            return {
                "ok": True, "backup": str(backup), "session_count": len(rows),
                "active_count": len(active),
                "archived_count": sum(row["deleted_at"] in (None, -1) and row["status"] == "archived"
                                      for row in rows),
                "rebound_count": rebound, "pinned_count": len(pins),
                "source_uid": source_uid, "target_uid": target_uid,
                "session_ids_preserved": True, "transcripts_unchanged": True,
            }
    except FollowError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise FollowError("客户端会话跟随失败，原会话已保留") from exc
