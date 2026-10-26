"""一键接码注册账号路由

支持使用疾驰短信平台 (JichiSMS) API 自动取号、WorkBuddy 自动发码、接码读码、换取凭证入库全流程。
支持任意填写注册数量（如写1注册1个，写2注册2个），支持实体卡优先与自动换号重试。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Generator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import wb_login
from admin.db import SessionLocal, get_db
from admin.routers.login import _verify_and_store
from admin.security import require_admin
from admin.sms_client import (
    DEFAULT_ENGINE,
    DEFAULT_PROJECT_ID_A,
    DEFAULT_PROJECT_ID_B,
    DEFAULT_PROJECT_NAME_A,
    DEFAULT_PROJECT_NAME_B,
    DEFAULT_TOKEN,
    JichiSmsClient,
    SmsClientError,
)

_logger = logging.getLogger("admin.sms_register")

router = APIRouter(prefix="/api/sms-register", tags=["sms-register"])

# 虚拟运营商号段（腾讯 OneID 等大厂常会风控丢弃或拦截下发）
VIRTUAL_PREFIXES = ("170", "171", "162", "165", "167")


class AutoRegisterIn(BaseModel):
    token: Optional[str] = None
    engine: str = DEFAULT_ENGINE  # 默认引擎A专属卡池
    project_id: str = DEFAULT_PROJECT_ID_A
    project_name: str = DEFAULT_PROJECT_NAME_A
    count: int = 1  # 注册数量：填1注册1个，填2注册2个...
    interval_between: int = 3  # 每个账号之间的等待间隔（秒）
    card_type: str = "all"  # 139372 专属卡池全部可用
    isp: Optional[str] = ""
    province: Optional[str] = ""
    paragraph: Optional[str] = ""
    ascription: Optional[str] = ""
    max_wait_seconds: int = 60  # 单个号码默认60秒，超时自动换号
    max_retries_per_account: int = 5  # 单个账号最大换号重试次数
    poll_interval: int = 5


@router.get("/config")
def get_config(_: bool = Depends(require_admin)):
    """获取接码预置配置与推荐项目"""
    return {
        "default_token": DEFAULT_TOKEN,
        "default_engine": DEFAULT_ENGINE,
        "default_project_id": DEFAULT_PROJECT_ID_A,
        "default_project_name": DEFAULT_PROJECT_NAME_A,
        "preset_projects": [
            {
                "engine": "a",
                "id": "139372",
                "name": "workbuddy/codebuddy/腾讯科技二次",
                "desc": "引擎A WorkBuddy/CodeBuddy 专属卡池（实测秒接码）",
            },
            {
                "engine": "b",
                "id": "77560",
                "name": "深圳市腾讯计算机系统",
                "desc": "引擎B 腾讯统一账号备用通道",
            },
            {
                "engine": "a",
                "id": "142759",
                "name": "腾讯科技/wegame",
                "desc": "引擎A 腾讯科技通道",
            },
            {
                "engine": "b",
                "id": "71139",
                "name": "腾讯云-注册",
                "desc": "引擎B 腾讯云注册通道",
            },
        ],
        "engines": [
            {
                "id": "a",
                "name": "引擎A (官方国内卡·推荐)",
                "default_project_id": "139372",
                "default_project_name": "workbuddy/codebuddy/腾讯科技二次",
            },
            {
                "id": "b",
                "name": "引擎B (国内卡)",
                "default_project_id": "77560",
                "default_project_name": "深圳市腾讯计算机系统",
            },
        ],
    }


@router.get("/projects")
def list_projects(
    keyword: str = Query("workbuddy"),
    engine: str = Query("a"),
    token: Optional[str] = Query(None),
    _: bool = Depends(require_admin),
):
    """搜索接码项目列表"""
    client = JichiSmsClient(token=token or DEFAULT_TOKEN)
    try:
        data = client.search_projects(keyword=keyword, engine=engine)
        return {"ok": True, "projects": data}
    except Exception as e:
        _logger.warning("搜索接码项目失败: %s", e)
        return {"ok": False, "message": str(e), "projects": []}


def _sse_event(event_name: str, payload: dict[str, Any]) -> str:
    """构造标准 SSE 数据包"""
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _register_single_account(
    client: JichiSmsClient,
    engine: str,
    project_id: str,
    project_name: str,
    body: AutoRegisterIn,
    current_index: int,
    total_count: int,
) -> Generator[str, None, dict[str, Any]]:
    """单个账号接码注册流程（带自动换号重试机制）"""
    max_retries = max(1, min(body.max_retries_per_account, 5))
    max_wait = max(20, min(body.max_wait_seconds, 120))
    interval = max(2, min(body.poll_interval, 6))

    for attempt in range(1, max_retries + 1):
        phone = ""
        sess = None
        login_success = False
        attempt_prefix = f"[{current_index}/{total_count}]" + (f" (尝试 {attempt}/{max_retries})" if max_retries > 1 else "")

        try:
            # 1. 取号
            yield _sse_event("step", {
                "index": current_index,
                "total": total_count,
                "attempt": attempt,
                "max_attempts": max_retries,
                "step": "get_phone",
                "status": "running",
                "message": f"{attempt_prefix} 正在向接码平台申请手机号 (引擎{engine.upper()})...",
            })

            # 如果用户指定实体卡优先，在参数中指定运营商
            isp_param = body.isp or ""
            ascription_param = body.ascription or ""
            if body.card_type == "physical" and engine == "a":
                if not isp_param and not ascription_param:
                    isp_param = "移动"

            phone_data = None
            last_err = ""
            for get_try in range(1, 4):
                try:
                    phone_data = client.get_phone(
                        engine=engine,
                        project_id=project_id,
                        project_name=project_name,
                        isp=isp_param,
                        province=body.province or "",
                        paragraph=body.paragraph or "",
                        ascription=ascription_param,
                    )
                    if phone_data and phone_data.get("phone"):
                        p = phone_data["phone"]
                        # 如果是实体卡模式，检测到 170/171 虚拟号则释放并重选
                        if body.card_type == "physical" and any(p.startswith(vp) for vp in VIRTUAL_PREFIXES):
                            _logger.info("获取到虚拟号 %s，释放并重新申请实体卡...", p)
                            client.release_phone(phone=p, project_id=project_id, engine=engine)
                            phone_data = None
                            time.sleep(2)
                            continue
                        break
                except Exception as e:
                    last_err = str(e)
                    if get_try < 3:
                        time.sleep(2)

            if not phone_data or not phone_data.get("phone"):
                msg = f"获取手机号失败: {last_err or '暂无可用号码，请稍后重试'}"
                yield _sse_event("step", {
                    "index": current_index,
                    "total": total_count,
                    "step": "get_phone",
                    "status": "error",
                    "message": f"{attempt_prefix} {msg}",
                })
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                return {"ok": False, "error": msg, "phone": ""}

            phone = phone_data["phone"]
            is_virtual = any(phone.startswith(vp) for vp in VIRTUAL_PREFIXES)
            type_label = "虚拟号段" if is_virtual else "实体号段"

            yield _sse_event("step", {
                "index": current_index,
                "total": total_count,
                "attempt": attempt,
                "step": "get_phone",
                "status": "success",
                "phone": phone,
                "message": f"{attempt_prefix} 成功获取手机号: {phone} ({type_label}" + (f" {phone_data['sp']} {phone_data['phone_gsd']})" if phone_data.get("sp") else ")"),
            })

            # 2. 初始化 WorkBuddy 登录会话
            yield _sse_event("step", {
                "index": current_index,
                "total": total_count,
                "attempt": attempt,
                "step": "init_session",
                "status": "running",
                "phone": phone,
                "message": f"{attempt_prefix} 正在初始化 WorkBuddy 登录会话...",
            })

            try:
                sess = wb_login.start_login()
            except wb_login.WbLoginError as e:
                client.release_phone(phone=phone, project_id=project_id, engine=engine)
                msg = f"初始化会话失败: {e.message}"
                yield _sse_event("step", {
                    "index": current_index,
                    "total": total_count,
                    "step": "init_session",
                    "status": "error",
                    "phone": phone,
                    "message": f"{attempt_prefix} {msg}",
                })
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                return {"ok": False, "error": msg, "phone": phone}

            yield _sse_event("step", {
                "index": current_index,
                "total": total_count,
                "attempt": attempt,
                "step": "init_session",
                "status": "success",
                "phone": phone,
                "sid": sess.sid,
                "message": f"{attempt_prefix} WorkBuddy 登录会话就绪",
            })

            # 3. 发送验证码短信
            yield _sse_event("step", {
                "index": current_index,
                "total": total_count,
                "attempt": attempt,
                "step": "send_sms",
                "status": "running",
                "phone": phone,
                "message": f"{attempt_prefix} 正在向 {phone} 发送验证码短信...",
            })

            try:
                wb_login.send_sms(sess, phone, "86")
            except wb_login.WbLoginError as e:
                client.release_phone(phone=phone, project_id=project_id, engine=engine)
                msg = f"发送短信失败: {e.message}"
                yield _sse_event("step", {
                    "index": current_index,
                    "total": total_count,
                    "step": "send_sms",
                    "status": "error",
                    "phone": phone,
                    "message": f"{attempt_prefix} {msg}",
                })
                if attempt < max_retries:
                    yield _sse_event("step", {
                        "index": current_index,
                        "total": total_count,
                        "step": "rotate",
                        "status": "running",
                        "message": f"{attempt_prefix} 号码被风控或发送失败，正在自动更换号码...",
                    })
                    time.sleep(2)
                    continue
                return {"ok": False, "error": msg, "phone": phone}
            except Exception as e:
                client.release_phone(phone=phone, project_id=project_id, engine=engine)
                msg = f"发送短信网络异常: {e}"
                yield _sse_event("step", {
                    "index": current_index,
                    "total": total_count,
                    "step": "send_sms",
                    "status": "error",
                    "phone": phone,
                    "message": f"{attempt_prefix} {msg}",
                })
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                return {"ok": False, "error": msg, "phone": phone}

            yield _sse_event("step", {
                "index": current_index,
                "total": total_count,
                "attempt": attempt,
                "step": "send_sms",
                "status": "success",
                "phone": phone,
                "message": f"{attempt_prefix} 验证码短信已下发至 {phone}，开始轮询接码...",
            })

            # 4. 轮询读取验证码
            start_time = time.time()
            code = ""

            yield _sse_event("step", {
                "index": current_index,
                "total": total_count,
                "attempt": attempt,
                "step": "poll_code",
                "status": "running",
                "phone": phone,
                "elapsed": 0,
                "max_wait": max_wait,
                "message": f"{attempt_prefix} 等待短信到达 (最长等待 {max_wait}s)...",
            })

            while time.time() - start_time < max_wait:
                time.sleep(interval)
                elapsed = int(time.time() - start_time)

                try:
                    code_res = client.get_verify_code(
                        phone=phone,
                        project_id=project_id,
                        engine=engine,
                    )
                except Exception as e:
                    code_res = {"received": False, "msg": str(e)}

                if code_res.get("received") and code_res.get("code"):
                    code = str(code_res["code"]).strip()
                    break

                # 实时推送倒计时与等待信息
                server_msg = code_res.get("msg") or "等待"
                yield _sse_event("step", {
                    "index": current_index,
                    "total": total_count,
                    "attempt": attempt,
                    "step": "poll_code",
                    "status": "waiting",
                    "phone": phone,
                    "elapsed": elapsed,
                    "max_wait": max_wait,
                    "message": f"{attempt_prefix} 等待短信中... (已等待 {elapsed}s · {server_msg})",
                })

            # 如果本次未收到验证码
            if not code:
                client.release_phone(phone=phone, project_id=project_id, engine=engine)
                # 还有重试机会，自动换号！
                if attempt < max_retries:
                    yield _sse_event("step", {
                        "index": current_index,
                        "total": total_count,
                        "attempt": attempt,
                        "step": "rotate",
                        "status": "running",
                        "phone": phone,
                        "message": f"{attempt_prefix} 号码 {phone} 超过 {max_wait}s 未收到短信（可能受运营商风控），已自动释放并换号重试 (第 {attempt+1}/{max_retries} 次)...",
                    })
                    time.sleep(2)
                    continue

                msg = f"号码 {phone} 等待 {max_wait}s 未收到短信，已自动释放号码并取消扣费"
                yield _sse_event("step", {
                    "index": current_index,
                    "total": total_count,
                    "step": "poll_code",
                    "status": "error",
                    "phone": phone,
                    "message": f"{attempt_prefix} {msg}",
                })
                return {"ok": False, "error": msg, "phone": phone}

            # 收到验证码！
            yield _sse_event("step", {
                "index": current_index,
                "total": total_count,
                "attempt": attempt,
                "step": "poll_code",
                "status": "success",
                "phone": phone,
                "code": code,
                "message": f"{attempt_prefix} 成功收到短信验证码: {code}",
            })

            # 5. 换取凭证并入库
            yield _sse_event("step", {
                "index": current_index,
                "total": total_count,
                "attempt": attempt,
                "step": "verify_login",
                "status": "running",
                "phone": phone,
                "message": f"{attempt_prefix} 正在校验凭证并写入账号池...",
            })

            db = SessionLocal()
            try:
                out = _verify_and_store(sess, code, db)
                login_success = True
            except Exception as e:
                _logger.exception("验证码校验入库异常: %s", e)
                detail = getattr(e, "detail", None) or str(e)
                if isinstance(detail, dict):
                    detail = detail.get("message") or str(detail)
                msg = f"授权入库失败: {detail}"
                yield _sse_event("step", {
                    "index": current_index,
                    "total": total_count,
                    "step": "verify_login",
                    "status": "error",
                    "phone": phone,
                    "message": f"{attempt_prefix} {msg}",
                })
                return {"ok": False, "error": msg, "phone": phone}
            finally:
                db.close()

            acc = out.get("account") or {}
            uid = out.get("uid") or ""
            duplicated = out.get("duplicated", False)

            yield _sse_event("step", {
                "index": current_index,
                "total": total_count,
                "attempt": attempt,
                "step": "verify_login",
                "status": "success",
                "phone": phone,
                "account": acc,
                "uid": uid,
                "duplicated": duplicated,
                "message": f"{attempt_prefix} 入库成功！账号: {acc.get('name', '未命名')} · UID: {uid}",
            })

            # 6. 收尾释放号码
            client.release_phone(phone=phone, project_id=project_id, engine=engine)

            return {
                "ok": True,
                "phone": phone,
                "uid": uid,
                "account": acc,
                "duplicated": duplicated,
            }

        except Exception as e:
            _logger.exception("注册流程发生未捕获异常: %s", e)
            msg = f"发生意外异常: {e}"
            if attempt < max_retries:
                time.sleep(2)
                continue
            return {"ok": False, "error": msg, "phone": phone}
        finally:
            if phone and not login_success:
                try:
                    client.release_phone(phone=phone, project_id=project_id, engine=engine)
                except Exception:
                    pass

    return {"ok": False, "error": f"重试 {max_retries} 次仍未成功接收验证码", "phone": ""}


def _execute_auto_register_batch(
    body: AutoRegisterIn,
) -> Generator[str, None, None]:
    """批量一键接码注册流式生成器"""
    token = (body.token or DEFAULT_TOKEN).strip()
    if not token:
        yield _sse_event("error", {"message": "未配置接码平台 API Token"})
        return

    client = JichiSmsClient(token=token)
    engine = body.engine.lower().strip() or "a"
    project_id = str(body.project_id).strip() or DEFAULT_PROJECT_ID_A
    project_name = body.project_name.strip() or DEFAULT_PROJECT_NAME_A

    count = max(1, min(body.count, 50))
    interval_between = max(1, min(body.interval_between, 30))

    yield _sse_event("batch_start", {
        "total": count,
        "engine": engine,
        "project_id": project_id,
        "project_name": project_name,
        "card_type": body.card_type,
        "message": f"开始执行一键接码注册，目标数量: {count} 个账号 (引擎{engine.upper()})",
    })

    success_accounts: list[dict[str, Any]] = []
    failed_accounts: list[dict[str, Any]] = []

    for idx in range(1, count + 1):
        yield _sse_event("account_start", {
            "index": idx,
            "total": count,
            "message": f"▶ 正在进行第 {idx}/{count} 个账号注册...",
        })

        gen = _register_single_account(
            client=client,
            engine=engine,
            project_id=project_id,
            project_name=project_name,
            body=body,
            current_index=idx,
            total_count=count,
        )

        res = None
        while True:
            try:
                evt = next(gen)
                yield evt
            except StopIteration as stop:
                res = stop.value
                break

        if res and res.get("ok"):
            success_accounts.append(res)
            yield _sse_event("account_done", {
                "index": idx,
                "total": count,
                "ok": True,
                "phone": res.get("phone", ""),
                "account": res.get("account", {}),
                "uid": res.get("uid", ""),
                "message": f"第 {idx}/{count} 个账号注册成功！手机号: {res.get('phone')} (昵称: {res.get('account', {}).get('name', '-')})",
            })
        else:
            err = (res and res.get("error")) or "注册失败"
            failed_accounts.append({"index": idx, "phone": (res and res.get("phone")) or "", "error": err})
            yield _sse_event("account_fail", {
                "index": idx,
                "total": count,
                "ok": False,
                "phone": (res and res.get("phone")) or "",
                "error": err,
                "message": f"第 {idx}/{count} 个账号未成功: {err}",
            })

        if idx < count:
            yield _sse_event("batch_wait", {
                "index": idx,
                "total": count,
                "wait_seconds": interval_between,
                "message": f"第 {idx} 个处理完毕，等待 {interval_between} 秒后继续第 {idx+1} 个...",
            })
            time.sleep(interval_between)

    yield _sse_event("batch_done", {
        "total": count,
        "success_count": len(success_accounts),
        "failed_count": len(failed_accounts),
        "success_accounts": success_accounts,
        "failed_accounts": failed_accounts,
        "message": f"全部处理完毕！共申请 {count} 个，成功入库 {len(success_accounts)} 个，失败 {len(failed_accounts)} 个。",
    })


@router.post("/run")
def run_auto_register(
    body: AutoRegisterIn,
    request: Request,
    _: bool = Depends(require_admin),
):
    """一键接码注册（SSE 流式端点，支持指定数量与自动换号重试）"""
    return StreamingResponse(
        _execute_auto_register_batch(body),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
