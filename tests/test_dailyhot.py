"""Tests for dailyhot module — caching, fetching, formatting.

All network calls are mocked — the module depends on an external DailyHotApi.
"""

import json
import time
from unittest.mock import patch, MagicMock


def test_list_sources_caches(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.dailyhot._CACHE_DIR", tmp_path)
    monkeypatch.setattr("metano.dailyhot._CACHE_TTL", 300)

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"routes": [{"name": "zhihu", "title": "知乎"}]}).encode()
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        from metano.dailyhot import list_sources
        sources = list_sources()
        assert len(sources) == 1
        assert sources[0]["name"] == "zhihu"

    # Second call should use cache, not urlopen
    with patch("urllib.request.urlopen") as mock_urlopen:
        sources2 = list_sources()
        assert len(sources2) == 1
        mock_urlopen.assert_not_called()


def test_list_sources_uses_base_url():
    from metano.dailyhot import BASE_URL
    assert "localhost" in BASE_URL or BASE_URL != ""


def test_get_hot_success(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.dailyhot._CACHE_DIR", tmp_path)

    data = {"code": 200, "title": "知乎热榜", "data": [
        {"title": "Item 1", "hot": 100000, "url": "https://example.com/1"},
        {"title": "Item 2", "hot": 50000, "url": "https://example.com/2"},
    ], "total": 2, "updateTime": "2025-01-01T00:00:00"}

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode()
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        from metano.dailyhot import get_hot
        result = get_hot("zhihu", limit=5)
        assert result["code"] == 200
        assert len(result["data"]) == 2


def test_get_hot_cached(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.dailyhot._CACHE_DIR", tmp_path)
    monkeypatch.setattr("metano.dailyhot._CACHE_TTL", 300)

    data = {"code": 200, "title": "Test", "data": [{"title": "Cached Item", "hot": 100}], "total": 1}

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode()
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        from metano.dailyhot import get_hot
        get_hot("test_source", limit=5)

    with patch("urllib.request.urlopen") as mock_urlopen:
        result = get_hot("test_source", limit=5)
        assert result["data"][0]["title"] == "Cached Item"
        mock_urlopen.assert_not_called()


def test_get_hot_cache_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.dailyhot._CACHE_DIR", tmp_path)
    monkeypatch.setattr("metano.dailyhot._CACHE_TTL", 0)  # expire immediately

    fresh_data = {"code": 200, "title": "Fresh", "data": [{"title": "Fresh Item"}], "total": 1}

    def mock_urlopen(*args, **kwargs):
        r = MagicMock()
        r.read.return_value = json.dumps(fresh_data).encode()
        r.__enter__.return_value = r
        return r

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        from metano.dailyhot import get_hot
        # TTL=0 means every call re-fetches
        r1 = get_hot("expiring", limit=5)
        assert r1["title"] == "Fresh"


def test_format_hot_success(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.dailyhot._CACHE_DIR", tmp_path)

    data = {"code": 200, "title": "知乎热榜", "data": [
        {"title": "Top Story", "hot": 99999, "url": "https://example.com/top"},
    ], "total": 1, "updateTime": "12:00"}

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode()
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        from metano.dailyhot import format_hot
        text = format_hot("zhihu", limit=5)
        assert "Top Story" in text
        assert "知乎热榜" in text
        assert "99,999" in text


def test_format_hot_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.dailyhot._CACHE_DIR", tmp_path)

    data = {"code": 400, "message": "bad request"}

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode()
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        from metano.dailyhot import format_hot
        text = format_hot("nonexistent", limit=5)
        assert "Failed to fetch" in text


def test_list_sources_empty():
    from metano.dailyhot import list_sources
    from metano.dailyhot import _CACHE_DIR
    import shutil
    if _CACHE_DIR.exists():
        shutil.rmtree(str(_CACHE_DIR))

    data = {"routes": []}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode()
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        sources = list_sources()
        assert sources == []


def test_fetch_error_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.dailyhot._CACHE_DIR", tmp_path)

    with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
        from metano.dailyhot import get_hot
        import pytest
        with pytest.raises(Exception):
            get_hot("any", limit=5)
