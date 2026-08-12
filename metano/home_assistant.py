"""Smart home/IoT integration via Home Assistant REST API."""
import json
import os
import re
import time
from typing import Optional
import requests
from metano.log import logger
from .paths import CONFIG_PATH

_ENTITY_ID_RE = re.compile(r'^[a-z][a-z0-9_]*\.[a-z0-9_]+$')


def _validate_entity_id(entity_id: str) -> bool:
    """Return True if entity_id is a safe HA entity identifier.

    SECURITY: entity_id is interpolated into the HA REST URL path. A value like
    '../config' or 'switch.1/../../states' would traverse to arbitrary HA
    endpoints (carrying the stored bearer token). Only allow domain.entity form.
    """
    return bool(_ENTITY_ID_RE.match(entity_id))

def _get_ha_config() -> dict:
    """Load Home Assistant config from gateway_config.yaml."""
    try:
        import yaml
        config_path = CONFIG_PATH
        if config_path.exists():
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            return config.get('home_assistant', {})
    except Exception:
        logger.exception()
    return {}


def _validate_ha_base_url(base_url: str) -> str | None:
    """Return an error message if the HA base_url is not a safe http(s) URL.

    SECURITY (H-09): the base_url carries the HA bearer token when requests are
    made; only allow http/https with a plain host so the token can never be
    sent to a file:/data:/javascript: or other scheme handler.
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base_url.strip())
    except ValueError:
        return f'Invalid Home Assistant url: {base_url}'
    if parsed.scheme not in ('http', 'https'):
        return f'Home Assistant url must use http/https, got: {parsed.scheme or "(none)"}'
    if not parsed.hostname or parsed.username or parsed.password:
        return f'Home Assistant url must have a plain host: {base_url}'
    return None


def _ha_request(method: str, path: str, data: dict=None) -> dict:
    """Make a request to Home Assistant REST API."""
    config = _get_ha_config()
    base_url = config.get('url', os.environ.get('HA_URL', 'http://homeassistant.local:8123'))
    token = config.get('token', os.environ.get('HA_TOKEN', ''))
    if not token:
        return {'error': 'Home Assistant not configured. Set HA_URL and HA_TOKEN in gateway_config.yaml or environment.'}
    url_err = _validate_ha_base_url(base_url)
    if url_err:
        return {'error': url_err}
    url = f'{base_url}/api{path}'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    try:
        if method == 'GET':
            resp = requests.get(url, headers=headers, timeout=10)
        elif method == 'POST':
            resp = requests.post(url, headers=headers, json=data, timeout=10)
        else:
            return {'error': f'Unsupported method: {method}'}
        resp.raise_for_status()
        return resp.json() if resp.text else {}
    except requests.exceptions.ConnectionError:
        return {'error': f'Cannot connect to Home Assistant at {base_url}'}
    except requests.exceptions.HTTPError as e:
        return {'error': f'HA API error: {e.response.status_code}'}
    except Exception as e:
        logger.exception()
        return {'error': str(e)}

def home_control(entity_id: str, action: str, value: str='') -> dict:
    """Control a Home Assistant entity.

    action: turn_on, turn_off, toggle, set_value
    value: optional value for set_value action (e.g., brightness, temperature)
    """
    if not _validate_entity_id(entity_id):
        return {'error': f'Invalid entity_id: {entity_id}. Expected domain.entity (e.g. light.living_room)'}
    domain = entity_id.split('.')[0]
    if action == 'turn_on':
        service = 'turn_on'
        data = {'entity_id': entity_id}
        if value:
            try:
                data['brightness'] = int(float(value))
            except ValueError:
                pass
    elif action == 'turn_off':
        service = 'turn_off'
        data = {'entity_id': entity_id}
    elif action == 'toggle':
        service = 'toggle'
        data = {'entity_id': entity_id}
    elif action == 'set_value':
        service = 'set_value'
        data = {'entity_id': entity_id, 'value': value}
    else:
        return {'error': f'Unknown action: {action}. Use turn_on, turn_off, toggle, set_value'}
    result = _ha_request('POST', f'/services/{domain}/{service}', data)
    return {'entity_id': entity_id, 'action': action, 'result': result}

def home_status(entity_id: str='') -> dict:
    """Get status of Home Assistant entities."""
    if entity_id:
        if not _validate_entity_id(entity_id):
            return {'error': f'Invalid entity_id: {entity_id}. Expected domain.entity'}
        result = _ha_request('GET', f'/states/{entity_id}')
        if 'error' in result:
            return result
        return {'entity_id': result.get('entity_id', ''), 'state': result.get('state', ''), 'attributes': result.get('attributes', {})}
    else:
        result = _ha_request('GET', '/states')
        if isinstance(result, list):
            entities = {}
            for e in result:
                eid = e.get('entity_id', '')
                domain = eid.split('.')[0]
                entities.setdefault(domain, []).append({'entity_id': eid, 'state': e.get('state', '')})
            return {'domains': {k: len(v) for k, v in entities.items()}, 'total': len(result)}
        return result

def home_automate(name: str, trigger: dict, actions: list[dict]) -> dict:
    """Create a simple automation in Home Assistant."""
    # SECURITY: name is interpolated into the URL path — reject path traversal.
    if not name or '/' in name or '..' in name or '\\' in name:
        return {'error': 'Invalid automation name (must not contain path separators)'}
    automation = {'alias': name, 'trigger': trigger, 'action': actions}
    result = _ha_request('POST', '/config/automation/config/' + name, automation)
    return {'name': name, 'result': result}

def get_all_entities() -> list[dict]:
    """Get all HA entities with full state info."""
    result = _ha_request('GET', '/states')
    if isinstance(result, list):
        return [{'entity_id': e.get('entity_id', ''), 'state': e.get('state', ''), 'attributes': e.get('attributes', {})} for e in result]
    if isinstance(result, dict) and 'error' in result:
        return []
    return []

def get_entity_state(entity_id: str) -> dict:
    """Get state for a specific entity."""
    # SECURITY (H-09): entity_id is interpolated into the HA REST URL path which
    # is requested with the stored bearer token. Reuse the same strict
    # domain.entity validation as home_status/home_control so '..', '/', '\'
    # or encoded path separators can never redirect the token to another HA API.
    if not _validate_entity_id(entity_id):
        return {'error': f'Invalid entity_id: {entity_id}. Expected domain.entity'}
    return _ha_request('GET', f'/states/{entity_id}')

def ha_is_configured() -> bool:
    """Return True when Home Assistant credentials are configured (token present)."""
    config = _get_ha_config()
    token = config.get('token') or os.environ.get('HA_TOKEN', '')
    return bool(token)

def home_status_full() -> dict:
    """Full status for the smart home page.

    Always includes a ``configured`` flag so the frontend can distinguish
    "Home Assistant not configured" from "configured but no devices".
    """
    if not ha_is_configured():
        return {
            'configured': False,
            'entities': [],
            'message': '未配置 Home Assistant。请在 gateway_config.yaml 的 home_assistant 段，或环境变量 HA_URL / HA_TOKEN 中配置后重试。',
        }
    result = _ha_request('GET', '/states')
    if isinstance(result, list):
        entities = [
            {'entity_id': e.get('entity_id', ''), 'state': e.get('state', ''), 'attributes': e.get('attributes', {})}
            for e in result
        ]
        return {'configured': True, 'entities': entities}
    if isinstance(result, dict) and 'error' in result:
        return {'configured': True, 'entities': [], 'error': result['error']}
    return {'configured': True, 'entities': []}

def ha_get_config() -> dict:
    """Return current HA config without leaking the token."""
    config = _get_ha_config()
    url = config.get('url') or os.environ.get('HA_URL', '')
    token = config.get('token') or os.environ.get('HA_TOKEN', '')
    return {'url': url, 'token_set': bool(token)}

def ha_set_config(url: str, token: str) -> dict:
    """Persist Home Assistant url/token into gateway_config.yaml (merging existing keys).

    Writing to disk is atomic (write to a temp file then rename).
    """
    import tempfile
    import yaml
    path = CONFIG_PATH
    existing = {}
    if path.exists():
        with open(path) as f:
            existing = yaml.safe_load(f) or {}
    if not isinstance(existing, dict):
        existing = {}
    ha = dict(existing.get('home_assistant') or {})
    if url:
        ha['url'] = url
    if token:
        ha['token'] = token
    existing['home_assistant'] = ha
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='gateway_config.', suffix='.yaml')
    try:
        with os.fdopen(fd, 'w') as f:
            yaml.safe_dump(existing, f, allow_unicode=True, sort_keys=False)
        # SECURITY (M-08): config holds the HA token — force owner-only perms
        # (os.replace preserves the temp file's mode, so chmod before rename).
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return ha_get_config()