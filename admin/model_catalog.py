"""Short-lived catalog cache for discovery and automatic model selection."""
from collections import OrderedDict
import hashlib
import threading
import time

_cache = OrderedDict()
_lock = threading.Lock()
TTL_SECONDS = 300

# 上游「自动择优」聚合模型。
# 客户端界面（5.8.3 起）把它显示为 "Workbuddy-Auto"，但聊天接口
# /v2/chat/completions 里上游注册的模型 id 仍然是 "auto" —— 实测发
# "Workbuddy-Auto" 会被上游以 11102「model service info not found」拒绝。
# 因此：对外（客户端请求、模型展示）新旧名都接受；发给上游的一律是
# "auto"。将来上游真换了接口 id，只需改这里一处。
AUTO_MODEL_ID = "auto"
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
