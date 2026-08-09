"""Bridge to external MCP tools (Tavily, etc.) and internal MCP server tools."""
import json
import os
from pathlib import Path
from typing import Any
from metano.log import logger

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
    import urllib.request
    import urllib.error
    url = 'https://api.tavily.com/search'
    payload = json.dumps({'api_key': api_key, 'query': query, 'max_results': limit, 'include_answer': True}).encode()
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    try:
        proxies = {}
        proxy_url = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY') or 'http://127.0.0.1:7897'
        proxies = {'https': proxy_url, 'http': proxy_url}
        proxy_handler = urllib.request.ProxyHandler(proxies)
        opener = urllib.request.build_opener(proxy_handler)
        with opener.open(req, timeout=15) as resp:
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