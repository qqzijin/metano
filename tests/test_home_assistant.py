"""Tests for home_assistant module — entity control, status, error handling.

All HTTP calls are mocked — depends on external Home Assistant instance.
"""

from unittest.mock import patch, MagicMock


def _mock_ha_config(**kwargs):
    return patch("metano.home_assistant._get_ha_config", return_value=kwargs)


def test_home_control_turn_on():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"success": True}

    with _mock_ha_config(url="http://ha.local:8123", token="test_token"):
        with patch("requests.post", return_value=mock_resp):
            from metano.home_assistant import home_control
            r = home_control("light.bedroom", "turn_on")
            assert r["entity_id"] == "light.bedroom"
            assert r["action"] == "turn_on"
            assert "error" not in r


def test_home_control_turn_off():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"success": True}

    with _mock_ha_config(url="http://ha.local:8123", token="test_token"):
        with patch("requests.post", return_value=mock_resp):
            from metano.home_assistant import home_control
            r = home_control("switch.plug", "turn_off")
            assert r["action"] == "turn_off"
            assert "error" not in r


def test_home_control_toggle():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"success": True}

    with _mock_ha_config(url="http://ha.local:8123", token="test_token"):
        with patch("requests.post", return_value=mock_resp):
            from metano.home_assistant import home_control
            r = home_control("cover.garage", "toggle")
            assert r["action"] == "toggle"


def test_home_control_set_value():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"success": True}

    with _mock_ha_config(url="http://ha.local:8123", token="test_token"):
        with patch("requests.post", return_value=mock_resp):
            from metano.home_assistant import home_control
            r = home_control("climate.living_room", "set_value", "22.5")
            assert r["action"] == "set_value"


def test_home_control_unknown_action():
    from metano.home_assistant import home_control

    with _mock_ha_config(url="http://ha.local:8123", token="test_token"):
        r = home_control("light.test", "fly_away")
        assert "error" in r
        assert "Unknown action" in r["error"]


def test_home_control_no_token():
    from metano.home_assistant import home_control

    with _mock_ha_config(url="http://ha.local:8123", token=""):
        r = home_control("light.test", "turn_on")
        assert "error" in r["result"]
        assert "not configured" in r["result"]["error"].lower()


def test_home_status_single_entity():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "entity_id": "light.bedroom",
        "state": "on",
        "attributes": {"brightness": 200, "friendly_name": "Bedroom Light"},
    }

    with _mock_ha_config(url="http://ha.local:8123", token="test_token"):
        with patch("requests.get", return_value=mock_resp):
            from metano.home_assistant import home_status
            r = home_status("light.bedroom")
            assert r["entity_id"] == "light.bedroom"
            assert r["state"] == "on"
            assert r["attributes"]["brightness"] == 200


def test_home_status_all():
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"entity_id": "light.a", "state": "on"},
        {"entity_id": "light.b", "state": "off"},
        {"entity_id": "switch.c", "state": "on"},
    ]

    with _mock_ha_config(url="http://ha.local:8123", token="test_token"):
        with patch("requests.get", return_value=mock_resp):
            from metano.home_assistant import home_status
            r = home_status()
            assert r["total"] == 3
            assert "light" in r["domains"]
            assert "switch" in r["domains"]


def test_home_status_connection_error():
    with _mock_ha_config(url="http://ha.local:8123", token="test_token"):
        with patch("requests.get", side_effect=__import__("requests").exceptions.ConnectionError):
            from metano.home_assistant import home_status
            r = home_status("light.test")
            assert "error" in r
            assert "Cannot connect" in r["error"]


def test_get_all_entities():
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"entity_id": "light.a", "state": "on", "attributes": {"friendly_name": "Light A"}},
    ]

    with _mock_ha_config(url="http://ha.local:8123", token="test_token"):
        with patch("requests.get", return_value=mock_resp):
            from metano.home_assistant import get_all_entities
            entities = get_all_entities()
            assert len(entities) == 1
            assert entities[0]["entity_id"] == "light.a"


def test_home_control_turn_on_with_brightness():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"success": True}

    with _mock_ha_config(url="http://ha.local:8123", token="test_token"):
        with patch("requests.post", return_value=mock_resp) as mock_post:
            from metano.home_assistant import home_control
            home_control("light.bedroom", "turn_on", "128")
            call_data = mock_post.call_args[1]["json"]
            assert call_data["brightness"] == 128


def test_home_automate():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"success": True}

    with _mock_ha_config(url="http://ha.local:8123", token="test_token"):
        with patch("requests.post", return_value=mock_resp):
            from metano.home_assistant import home_automate
            r = home_automate(
                "test_auto",
                {"platform": "state", "entity_id": "light.test"},
                [{"service": "light.turn_on", "target": {"entity_id": "light.test"}}],
            )
            assert r["name"] == "test_auto"
