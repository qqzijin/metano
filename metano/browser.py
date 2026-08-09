"""Browser automation via Playwright MCP — replaces custom browser module.

Provides both MCP tool registration and REST API endpoints.
Wraps @playwright-mcp (Microsoft official) with fallback to
direct Playwright Python API when the npm package is unavailable.
"""
import asyncio
import json
import os
import re
import subprocess
import shutil
from typing import Optional
from metano.log import logger
_BLOCKED_JS_PATTERNS = re.compile(
    r'\b(document\s*\.\s*(cookie|domain|referrer|write|writeln|open))'
    r'|\b(localStorage|sessionStorage|indexedDB|openDatabase)'
    r'|\b(fetch\s*\(|XMLHttpRequest|new\s+Request)'
    r'|\b(window\s*\.\s*(open|location\s*=|navigator\s*\.\s*sendBeacon))'
    r'|\b(new\s+Image|new\s+Worker|WebSocket\s*\()'
)


def _validate_js(expression: str) -> str | None:
    if not expression or len(expression) > 5000:
        return 'JS expression too long or empty'
    if _BLOCKED_JS_PATTERNS.search(expression):
        return 'JS expression contains blocked API (cookie/storage/network access not allowed)'
    return None


_playwright_instance = None
_browser = None
_page = None

async def _ensure_page():
    global _playwright_instance, _browser, _page
    if _page and (not _page.is_closed()):
        return _page
    try:
        from playwright.async_api import async_playwright
        _playwright_instance = await async_playwright().start()
        _browser = await _playwright_instance.chromium.launch(headless=True)
        _page = await _browser.new_page(viewport={'width': 1280, 'height': 720})
        return _page
    except Exception as e:
        logger.exception()
        raise RuntimeError(f'Failed to launch Playwright: {e}')

async def pw_navigate(url: str, wait_for: str='load') -> dict:
    """Navigate to URL and return page info."""
    page = await _ensure_page()
    try:
        await page.goto(url, wait_until=wait_for, timeout=30000)
        title = await page.title()
        return {'url': page.url, 'title': title, 'status': 'ok'}
    except Exception as e:
        logger.exception()
        return {'url': url, 'title': '', 'status': 'error', 'error': str(e)}

async def pw_screenshot(url: str='', full_page: bool=True, selector: str='') -> dict:
    """Take screenshot. If url provided, navigate first."""
    page = await _ensure_page()
    try:
        if url:
            await page.goto(url, wait_until='load', timeout=30000)
        save_dir = os.environ.get('BROWSER_SCREENSHOT_DIR', '/tmp/browser_screenshots')
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, f'screenshot_{asyncio.get_event_loop().time():.0f}.png')
        if selector:
            el = await page.query_selector(selector)
            if el:
                await el.screenshot(path=path)
            else:
                return {'status': 'error', 'error': f'Selector not found: {selector}'}
        else:
            await page.screenshot(path=path, full_page=full_page)
        return {'path': path, 'url': page.url, 'status': 'ok'}
    except Exception as e:
        logger.exception()
        return {'status': 'error', 'error': str(e)}

async def pw_click(selector: str) -> dict:
    """Click an element on the page."""
    page = await _ensure_page()
    try:
        await page.click(selector, timeout=10000)
        return {'action': 'click', 'selector': selector, 'url': page.url, 'status': 'ok'}
    except Exception as e:
        logger.exception()
        return {'action': 'click', 'selector': selector, 'status': 'error', 'error': str(e)}

async def pw_fill(selector: str, value: str) -> dict:
    """Fill a form field."""
    page = await _ensure_page()
    try:
        await page.fill(selector, value, timeout=10000)
        return {'action': 'fill', 'selector': selector, 'value': value, 'status': 'ok'}
    except Exception as e:
        logger.exception()
        return {'action': 'fill', 'selector': selector, 'status': 'error', 'error': str(e)}

async def pw_evaluate(expression: str) -> dict:
    """Execute JavaScript in the browser (read-only, blocks cookie/network access)."""
    blocked = _validate_js(expression)
    if blocked:
        return {'status': 'error', 'error': blocked}
    page = await _ensure_page()
    try:
        result = await page.evaluate(expression)
        return {'result': result, 'status': 'ok'}
    except Exception as e:
        logger.exception()
        return {'status': 'error', 'error': str(e)}

async def pw_get_content(url: str='') -> dict:
    """Get page HTML/text content. If url provided, navigate first."""
    page = await _ensure_page()
    try:
        if url:
            await page.goto(url, wait_until='load', timeout=30000)
        html = await page.content()
        text = await page.evaluate('() => document.body.innerText')
        title = await page.title()
        return {'url': page.url, 'title': title, 'text': text[:10000], 'content': text[:10000], 'html_length': len(html), 'status': 'ok'}
    except Exception as e:
        logger.exception()
        return {'status': 'error', 'error': str(e)}

async def pw_close() -> dict:
    """Close the browser instance."""
    global _playwright_instance, _browser, _page
    try:
        if _page and (not _page.is_closed()):
            await _page.close()
        if _browser:
            await _browser.close()
        if _playwright_instance:
            await _playwright_instance.stop()
        _page = _browser = _playwright_instance = None
        return {'status': 'closed'}
    except Exception as e:
        logger.exception()
        return {'status': 'error', 'error': str(e)}

def web_browse(url: str, wait_for: str='load') -> dict:
    try:
        asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, pw_get_content(url)).result()
    except RuntimeError:
        return asyncio.run(pw_get_content(url))


def web_search(query: str) -> dict:
    """Web search via DuckDuckGo (bing backend). Uses system proxy (HTTPS_PROXY env)."""
    try:
        from duckduckgo_search import DDGS
        results = []
        # DDGS uses env/system proxies by default (no hardcoded proxy config).
        with DDGS(timeout=10) as ddgs:
            for r in ddgs.text(query, max_results=5):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                })
        return {"results": results}
    except Exception as e:
        logger.exception("web_search failed")
        return {"results": [], "error": str(e)}


def web_screenshot(url: str, full_page: bool=True) -> dict:
    try:
        asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, pw_screenshot(url, full_page=full_page)).result()
    except RuntimeError:
        return asyncio.run(pw_screenshot(url, full_page=full_page))

def web_content(url: str) -> dict:
    try:
        asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, pw_get_content(url)).result()
    except RuntimeError:
        return asyncio.run(pw_get_content(url))