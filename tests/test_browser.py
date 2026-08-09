"""Tests for browser module — JS validation, sync wrappers, error handling.

Playwright async functions require a real browser; we test the
JS expression validator and sync wrapper patterns instead.
"""

import json
import pytest

try:
    import duckduckgo_search  # noqa: F401
    HAS_DDG = True
except ImportError:
    HAS_DDG = False


def test_validate_js_empty():
    from metano.browser import _validate_js
    assert _validate_js("") is not None
    assert _validate_js(None) is not None


def test_validate_js_too_long():
    from metano.browser import _validate_js
    assert _validate_js("x" * 5001) is not None


def test_validate_js_simple():
    from metano.browser import _validate_js
    assert _validate_js("1 + 1") is None
    assert _validate_js("document.title") is None


def test_validate_js_blocks_cookie():
    from metano.browser import _validate_js
    assert _validate_js("document.cookie") is not None
    assert _validate_js("document.domain") is not None


def test_validate_js_blocks_local_storage():
    from metano.browser import _validate_js
    assert _validate_js("localStorage.getItem('x')") is not None
    assert _validate_js("sessionStorage") is not None


def test_validate_js_blocks_fetch():
    from metano.browser import _validate_js
    assert _validate_js("fetch('/api/data')") is not None
    assert _validate_js("new XMLHttpRequest()") is not None


def test_validate_js_blocks_window_open():
    from metano.browser import _validate_js
    assert _validate_js("window.open('http://evil.com')") is not None
    # navigator.sendBeacon is only blocked when preceded by window.
    assert _validate_js("window.navigator.sendBeacon('/log')") is not None


@pytest.mark.skipif(not HAS_DDG, reason="duckduckgo_search not installed")
def test_web_search_sync_returns_results():
    from metano.browser import web_search

    with __import__("unittest").mock.patch(
        "duckduckgo_search.DDGS"
    ) as mock_ddgs:
        mock_ddgs.return_value.__enter__.return_value.text.return_value = [
            {"title": "Result 1", "href": "https://example.com/1", "body": "Snippet 1"},
            {"title": "Result 2", "href": "https://example.com/2", "body": "Snippet 2"},
        ]
        r = web_search("test query")
        assert len(r["results"]) == 2
        assert r["results"][0]["title"] == "Result 1"


@pytest.mark.skipif(not HAS_DDG, reason="duckduckgo_search not installed")
def test_web_search_error_returns_empty():
    from metano.browser import web_search

    with __import__("unittest").mock.patch(
        "duckduckgo_search.DDGS", side_effect=Exception("API error")
    ):
        r = web_search("test query")
        assert r["results"] == []
        assert "error" in r


def test_web_browse_no_running_loop():
    """When no running loop, fallback to asyncio.run()."""
    from metano.browser import web_browse

    with __import__("unittest").mock.patch(
        "metano.browser.asyncio.get_running_loop", side_effect=RuntimeError
    ):
        with __import__("unittest").mock.patch(
            "metano.browser.asyncio.run", return_value={"status": "ok", "url": "https://example.com"}
        ) as mock_run:
            r = web_browse("https://example.com")
            assert r["status"] == "ok"
            mock_run.assert_called_once()


def test_web_screenshot_no_running_loop():
    from metano.browser import web_screenshot

    with __import__("unittest").mock.patch(
        "metano.browser.asyncio.get_running_loop", side_effect=RuntimeError
    ):
        with __import__("unittest").mock.patch(
            "metano.browser.asyncio.run", return_value={"status": "ok", "path": "/tmp/test.png"}
        ):
            r = web_screenshot("https://example.com")
            assert r["status"] == "ok"


def test_web_content_no_running_loop():
    from metano.browser import web_content

    with __import__("unittest").mock.patch(
        "metano.browser.asyncio.get_running_loop", side_effect=RuntimeError
    ):
        with __import__("unittest").mock.patch(
            "metano.browser.asyncio.run", return_value={"status": "ok", "text": "hello"}
        ):
            r = web_content("https://example.com")
            assert r["status"] == "ok"
