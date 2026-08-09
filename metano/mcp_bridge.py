"""Bridge to external MCP tools (Tavily, etc.) and internal MCP server tools."""
import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any
from metano.log import logger

# Proxy fallback chain for outbound API calls. First reachable entry wins.
# Set HTTPS_PROXY/HTTP_PROXY env to override, or add your own proxies here.
_PROXY_CANDIDATES = [
    'http://127.0.0.1:7897',  # local proxy (default dev setup)
]


def _proxy_urls() -> list[str]:
    """Resolve ordered proxy candidates: explicit env first, then defaults."""
    env_proxy = (os.environ.get('HTTPS_PROXY')
                 or os.environ.get('HTTP_PROXY')
                 or os.environ.get('METANO_HTTP_PROXY', ''))
    if env_proxy:
        return [env_proxy] + _PROXY_CANDIDATES
    return list(_PROXY_CANDIDATES)


def _open_with_proxy(req, timeout: int = 15):
    """Open a request through the first reachable proxy in the chain.

    Raises the last error if every candidate fails.
    """
    last_err = None
    for proxy in _proxy_urls():
        try:
            proxies = {'https': proxy, 'http': proxy}
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
            return opener.open(req, timeout=timeout)
        except Exception as e:  # noqa: BLE001 - try next proxy
            last_err = e
    raise last_err if last_err else RuntimeError('no proxy candidates')

def _get_tavily_key() -> str:
    if os.environ.get('TAVILY_API_KEY'):
        return os.environ['TAVILY_API_KEY']
    mcp_path = Path.home() / '.mcp.json'
    if mcp_path.exists():
        try:
            cfg = json.loads(mcp_path.read_text())
            return cfg.get('mcpServers', {}).get('tavily', {}).get('env', {}).get('TAVILY_API_KEY', '')
        except Exception:
            logger.exception()
    return ''

async def tavily_search(query: str, limit: int=10) -> dict:
    api_key = _get_tavily_key()
    if not api_key:
        return {'error': 'TAVILY_API_KEY not configured', 'results': []}
    url = 'https://api.tavily.com/search'
    payload = json.dumps({'api_key': api_key, 'query': query, 'max_results': limit, 'include_answer': True}).encode()
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    try:
        with _open_with_proxy(req, timeout=15) as resp:
            data = json.loads(resp.read())
        results = [{'title': r.get('title', ''), 'url': r.get('url', ''), 'snippet': r.get('content', '')[:300], 'score': r.get('score', 0)} for r in data.get('results', [])]
        return {'query': query, 'answer': data.get('answer', ''), 'results': results}
    except urllib.error.URLError as e:
        return {'error': f'Tavily request failed: {e}', 'results': []}
    except Exception as e:
        logger.exception()
        return {'error': str(e), 'results': []}

async def list_available_tools() -> list[dict]:
    tools = []
    try:
        from .mcp_server import mcp
        if hasattr(mcp, '_tool_manager') and hasattr(mcp._tool_manager, '_tools'):
            for name, tool in mcp._tool_manager._tools.items():
                tools.append({'name': name, 'source': 'internal', 'description': (tool.description or '')[:200]})
        elif hasattr(mcp, '_tools'):
            for name, tool in mcp._tools.items():
                tools.append({'name': name, 'source': 'internal', 'description': (getattr(tool, 'description', '') or '')[:200]})
    except Exception as e:
        logger.exception()
    if _get_tavily_key():
        tools.append({'name': 'tavily_search', 'source': 'external', 'description': 'Web search via Tavily API'})
    return tools

async def call_tool(tool_name: str, args: dict) -> Any:
    if tool_name == 'tavily_search':
        return await tavily_search(args.get('query', ''), limit=args.get('limit', 10))
    try:
        from .mcp_server import mcp
        if tool_name in mcp._tools:
            import asyncio
            result = await mcp.call_tool(tool_name, args)
            return result
    except Exception:
        logger.exception()
    return {'error': f"Tool '{tool_name}' not found"}