"""DailyHot bridge: fetch trending/hot lists from local DailyHotApi (port 6688)."""

import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE_URL = os.environ.get("DAILYHOT_URL", "http://localhost:6688")
_TIMEOUT = int(os.environ.get("DAILYHOT_TIMEOUT", "5"))

_CACHE_DIR = Path.home() / ".claude" / "metano" / "cache"
_CACHE_TTL = 300  # 5 minutes


def _fetch(path: str, params: dict = None) -> dict | list:
    url = f"{BASE_URL}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        if qs:
            url += f"?{qs}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read())


def _cache_get(key: str):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = _CACHE_DIR / f"dailyhot_{key}.json"
    if f.exists() and (time.time() - f.stat().st_mtime) < _CACHE_TTL:
        return json.loads(f.read_text())
    return None


def _cache_set(key: str, data):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = _CACHE_DIR / f"dailyhot_{key}.json"
    f.write_text(json.dumps(data, ensure_ascii=False))


def list_sources() -> list[dict]:
    """Return all available hot-list sources with name, route, title."""
    cached = _cache_get("sources")
    if cached is not None:
        return cached
    data = _fetch("/all")
    routes = data.get("routes", [])
    _cache_set("sources", routes)
    return routes


def get_hot(source: str, limit: int = 10) -> dict:
    """Fetch hot list for a source. Returns {name, title, total, updateTime, data[]}."""
    cache_key = f"{source}_{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    result = _fetch(f"/{source}", {"limit": limit})
    _cache_set(cache_key, result)
    return result


def format_hot(source: str, limit: int = 10) -> str:
    """Fetch and format a hot list as readable text."""
    data = get_hot(source, limit)
    if not data or data.get("code") != 200:
        return f"Failed to fetch hot list for '{source}'"
    lines = [f"# {data.get('title', source)} 热榜 (共{data.get('total', '?')}条, 更新于{data.get('updateTime', '?')})"]
    for i, item in enumerate(data.get("data", [])[:limit], 1):
        hot_str = f" 🔥{item['hot']:,}" if item.get("hot") else ""
        lines.append(f"{i}. {item['title']}{hot_str}")
        if item.get("url"):
            lines.append(f"   {item['url']}")
    return "\n".join(lines)