"""In-memory holding area for a parsed lead list between upload and fire.

Nothing here is ever written to disk — an entry lives only in process RAM and
is pruned after TTL_SECONDS or process restart, which is exactly the
"transient list" behavior Lite wants (lists are used once, then gone).
"""
import threading
import time
import uuid

TTL_SECONDS = 3600

_CACHE: dict[str, dict] = {}
_LOCK = threading.Lock()


def _prune_locked() -> None:
    cutoff = time.time() - TTL_SECONDS
    for k in [k for k, v in _CACHE.items() if v["ts"] < cutoff]:
        _CACHE.pop(k, None)


def store(user_id: int, rows: list, columns: list) -> str:
    list_id = uuid.uuid4().hex[:12]
    with _LOCK:
        _prune_locked()
        _CACHE[list_id] = {"user_id": user_id, "rows": rows, "columns": columns, "ts": time.time()}
    return list_id


def get(user_id: int, list_id: str) -> dict | None:
    with _LOCK:
        entry = _CACHE.get(list_id)
        if not entry or entry["user_id"] != user_id:
            return None
        return entry
