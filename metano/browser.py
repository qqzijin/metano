"""Browser automation via Playwright MCP — replaces custom browser module.

Provides both MCP tool registration and REST API endpoints.
Wraps @playwright-mcp (Microsoft official) with fallback to
direct Playwright Python API when the npm package is unavailable.
"""
import asyncio
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional
from metano.log import logger

# ── SSRF guards (H-03) ─────────────────────────────────────────────────────
# Never navigate/read a URL that resolves to a private/loopback/link-local
# address (RFC 1918, link-local, loopback, CGNAT, cloud metadata 169.254.*,
# documentation ranges, multicast, reserved, IPv6 equivalents).
_PRIVATE_NETS = (
    ipaddress.ip_network('0.0.0.0/8'),
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('100.64.0.0/10'),
    ipaddress.ip_network('127.0.0.0/8'),
    ipaddress.ip_network('169.254.0.0/16'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.0.0.0/24'),
    ipaddress.ip_network('192.0.2.0/24'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('198.18.0.0/15'),
    ipaddress.ip_network('198.51.100.0/24'),
    ipaddress.ip_network('203.0.113.0/24'),
    ipaddress.ip_network('224.0.0.0/4'),
    ipaddress.ip_network('240.0.0.0/4'),
)


def _is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str.split('%')[0])
    except ValueError:
        return True  # unparseable — treat as unsafe
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified or any(ip in net for net in _PRIVATE_NETS))


def _validate_http_url(url: str) -> str | None:
    """Return an error string if ``url`` is not a safe http/https URL, else None."""
    if not url or not isinstance(url, str):
        return 'URL missing'
    if len(url) > 2048:
        return 'URL too long'
    try:
        parsed = urlparse(url)
    except ValueError:
        return f'Invalid URL: {url}'
    if parsed.scheme not in ('http', 'https'):
        return f'Only http/https URLs allowed, got: {parsed.scheme or "(none)"}'
    if not parsed.hostname:
        return 'URL missing host'
    if parsed.username or parsed.password:
        return 'URLs with embedded credentials are not allowed'
    return None


def _assert_public_host(hostname: str):
    """Raise ValueError/OSError unless every resolved address of hostname is public.

    Used before AND after navigation to mitigate DNS rebinding.
    """
    infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    addrs = [info[4][0] for info in infos]
    if not addrs:
        raise ValueError(f'Cannot resolve host: {hostname}')
    for a in addrs:
        if _is_private_ip(a):
            raise ValueError(f'Refusing to navigate to non-public address {a} for {hostname}')


# ── Screenshot storage (M-08) ─────────────────────────────────────────────
_SCREENSHOT_BASE = Path(tempfile.gettempdir()) / 'metano_screenshots'
_SCREENSHOT_TTL_SECONDS = 3600


def _cleanup_screenshot_dirs(base: Path):
    """Delete screenshot subdirs older than the TTL (best-effort)."""
    try:
        now = time.time()
        for child in base.iterdir():
            if not child.is_dir():
                continue
            try:
                if now - child.stat().st_mtime > _SCREENSHOT_TTL_SECONDS:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        pass


def _screenshot_dir() -> Path:
    """Create a fresh random private subdir for this request's screenshots."""
    base = Path(os.environ.get('BROWSER_SCREENSHOT_DIR', str(_SCREENSHOT_BASE)))
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    _cleanup_screenshot_dirs(base)
    d = base / secrets.token_hex(8)
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


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
    """Legacy shared page used only by the stateful MCP tools (click/fill/
    evaluate), which operate on the page left by the previous interaction.
    URL-navigating functions (screenshot/get_content/browse) no longer use this
    shared page — they use an isolated context per request (see _isolated_page)
    so cookies/storage are never shared between different users/requests."""
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


async def _isolated_page():
    """Yield a page in a brand-new browser context (no shared cookies/storage).

    Every request gets its own context; it is closed in ``finally`` so a
    screenshot of one site can never leak cookies to the next request.
    """
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(viewport={'width': 1280, 'height': 720})
    page = await context.new_page()

    # Defence-in-depth: block every sub-request that is non-http(s) or that
    # resolves to a private IP (guards against DNS rebinding mid-load).
    async def _route(route):
        rurl = route.request.url
        if rurl.startswith(('data:', 'file:', 'javascript:', 'about:', 'blob:')):
            await route.abort()
            return
        try:
            rp = urlparse(rurl)
            if rp.scheme in ('http', 'https') and rp.hostname:
                _assert_public_host(rp.hostname)
        except (ValueError, OSError):
            await route.abort()
            return
        await route.continue_()

    await page.route('**/*', _route)
    try:
        yield page
    finally:
        try:
            await context.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass


def _ssrf_check_url(url: str) -> str | None:
    """Validate scheme/host and assert the host resolves only to public IPs."""
    err = _validate_http_url(url)
    if err:
        return err
    hostname = urlparse(url).hostname
    try:
        _assert_public_host(hostname)
    except (ValueError, OSError) as e:
        return f'SSRF blocked: {e}'
    return None


async def pw_screenshot(url: str='', full_page: bool=True, selector: str='') -> dict:
    """Take screenshot. If url provided, navigate first.

    SECURITY (H-03): the URL must be http/https resolving only to public IPs
    (checked before and after navigation) and each request uses an isolated
    browser context so no cookies/state are shared between requests.
    """
    if url:
        err = _ssrf_check_url(url)
        if err:
            return {'status': 'error', 'error': err}
    try:
        async with _isolated_page() as page:
            if url:
                await page.goto(url, wait_until='load', timeout=30000)
                final = urlparse(page.url)
                if final.hostname:
                    try:
                        _assert_public_host(final.hostname)
                    except (ValueError, OSError) as e:
                        return {'status': 'error', 'error': f'SSRF blocked after navigation: {e}'}
            save_dir = _screenshot_dir()
            path = str(save_dir / f'screenshot_{asyncio.get_event_loop().time():.0f}.png')
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
    """Get page HTML/text content. If url provided, navigate first.

    Uses an isolated browser context and enforces the same SSRF rules as
    pw_screenshot (http/https + public-IP-only, checked before/after nav).
    """
    if url:
        err = _ssrf_check_url(url)
        if err:
            return {'status': 'error', 'error': err}
    try:
        async with _isolated_page() as page:
            if url:
                await page.goto(url, wait_until='load', timeout=30000)
                final = urlparse(page.url)
                if final.hostname:
                    try:
                        _assert_public_host(final.hostname)
                    except (ValueError, OSError) as e:
                        return {'status': 'error', 'error': f'SSRF blocked after navigation: {e}'}
            html = await page.content()
            text = await page.evaluate('() => document.body.innerText')
            title = await page.title()
            return {'url': page.url, 'title': title, 'text': text[:10000], 'content': text[:10000], 'html_length': len(html), 'status': 'ok'}
    except Exception as e:
        logger.exception()
        return {'status': 'error', 'error': str(e)}


def _fetch_static(url: str) -> dict:
    """Plain HTTP fetch (no JS rendering). Enforces the same SSRF rules as Playwright paths."""
    try:
        err = _validate_http_url(url)
        if err:
            return {'status': 'error', 'error': err}
        _assert_public_host(urlparse(url).hostname)
        import urllib.request
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(2_000_000)
            final = resp.geturl() or url
        text = raw.decode('utf-8', errors='replace')
        m = re.search(r'<title[^>]*>(.*?)</title>', text, re.S | re.I)
        title = m.group(1).strip()[:200] if m else ''
        return {'url': final, 'title': title, 'text': text[:10000], 'content': text[:10000],
                'html_length': len(text), 'status': 'ok', 'mode': 'static'}
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


def _fetch_stealth(url: str) -> dict:
    """Stealth-mode fetch via Scrapling StealthyFetcher; falls back to dynamic on any error."""
    try:
        from scrapling import StealthyFetcher
        page = StealthyFetcher.fetch(url)
        text = page.get_all_text() or ''
        html = page.html_content or ''
        m = re.search(r'<title[^>]*>(.*?)</title>', html, re.S | re.I)
        title = m.group(1).strip()[:200] if m else ''
        return {'url': page.url or url, 'title': title, 'text': text[:10000], 'content': text[:10000],
                'html_length': len(html), 'status': 'ok', 'mode': 'stealth'}
    except Exception as e:
        logger.warning('stealth fetch failed (%s); falling back to dynamic', e)
        return web_browse(url, mode='dynamic')


def _browse_dynamic(url: str) -> dict:
    try:
        asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, pw_get_content(url)).result()
    except RuntimeError:
        return asyncio.run(pw_get_content(url))


def web_browse(url: str, mode: str='dynamic', wait_for: str='load') -> dict:
    """Fetch a page. mode: static (plain HTTP) / dynamic (Playwright) / stealth (anti-bot)."""
    if mode == 'static':
        return _fetch_static(url)
    if mode == 'stealth':
        return _fetch_stealth(url)
    return _browse_dynamic(url)


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