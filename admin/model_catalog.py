"""Short-lived catalog cache for discovery and automatic model selection."""
from collections import OrderedDict
import hashlib
import threading
import time

_cache = OrderedDict()
_lock = threading.Lock()
TTL_SECONDS = 300

# 上游「自动择优」聚合模型。2026-10 起上游把模型名从 "auto" 改为 "Workbuddy-Auto"
# （客户端模型下拉里显示的就是这个名字）。旧名继续接受，避免老客户端与老配置
# （例如第三方客户端里存着 "auto"）在升级后直接报模型不存在；但任何发给上游的
# 请求都必须使用规范名，否则上游会判为未知模型。
AUTO_MODEL_ID = "Workbuddy-Auto"
AUTO_MODEL_ALIASES = frozenset({"auto", "workbuddy-auto"})


def is_auto_model(model_id) -> bool:
    """模型名是否指向「自动择优」聚合模型（规范名与旧别名、大小写都算）。"""
    return bool(model_id) and str(model_id).strip().lower() in AUTO_MODEL_ALIASES


def get_models(auth_json, backend_url, fetch):
    # Scope by credential/backend, without retaining raw credentials in memory.
    key = (backend_url, hashlib.sha256(auth_json.encode()).digest())
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and now < cached[0]:
            _cache.move_to_end(key)
            return cached[1]
    rows = fetch()
    if not rows:
        return rows
    with _lock:
        _cache[key] = (time.monotonic() + TTL_SECONDS, rows)
        _cache.move_to_end(key)
        while len(_cache) > 128:
            _cache.popitem(last=False)
    return rows


def auto_model(auth_json, backend_url, fetch):
    rows = get_models(auth_json, backend_url, fetch)
    # 聚合模型本身不参与「替 auto 挑一个具体模型」的候选，否则会挑回它自己。
    models = [row for row in rows if row.get('id') and not is_auto_model(row['id'])]
    if not models:
        return None
    free = [row for row in models if not row.get('credits') or
            'x0.00' in str(row['credits']) or 'free' in str(row['credits']).lower()]
    return (free or models)[0]['id']
