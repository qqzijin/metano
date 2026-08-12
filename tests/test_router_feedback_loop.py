"""End-to-end test: the Plan A feedback loop wired into gateway/router.py.

Drives the real ``route_message`` path (security check, session, prompt build,
the begin_route/end_route hooks) with a fake ``_call_claude`` so no CLI
subprocess is spawned and no real database is touched.
"""

import asyncio

import pytest

from metano import route_events, experience


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Redirect every DB / log / auth path touched by route_message to tmp."""
    monkeypatch.setattr(route_events, 'DB_PATH', tmp_path / 'evo.db')
    monkeypatch.setattr(experience, 'DB_PATH', tmp_path / 'evo.db')
    monkeypatch.setattr('metano.evo_models.EVO_DB_PATH', tmp_path / 'evo.db')
    monkeypatch.setattr('metano.db.DB_PATH', tmp_path / 'bridge.db')
    monkeypatch.setattr('metano.gateway.router.GATEWAY_LOG', tmp_path / 'gateway_log.jsonl')
    monkeypatch.setattr('metano.gateway.router.AUTH_STATE', tmp_path / 'authorizations.json')
    from metano import db as metano_db
    metano_db.init_db()
    route_events.init_db()
    route_events.set_enabled(True)
    return tmp_path


def _router(fake_response):
    from metano.gateway.router import MessageRouter

    async def fake_call_claude(self, prompt, session, skill_prefix='', allowed_tools=None,
                               skip_permissions=True, on_event=None, provider_name=''):
        return fake_response, 100, 50, 0

    MessageRouter._call_claude = fake_call_claude
    return MessageRouter()


def test_route_message_records_success(isolated):
    r = _router('正常回答内容')
    resp = asyncio.run(r.route_message('test', 'u1', '什么是机器学习？'))
    assert resp == '正常回答内容'
    stats = route_events.get_route_stats()
    assert stats['total_events'] == 1
    assert stats['by_outcome'].get('success') == 1


def test_route_message_failure_records_and_reflects(isolated):
    r = _router('Error: something failed')
    asyncio.run(r.route_message('test', 'u2', '帮我写个python函数'))
    stats = route_events.get_route_stats()
    assert stats['total_events'] == 1
    assert stats['by_outcome'].get('failure') == 1
    exp = experience.get_experience_stats()
    assert exp['total'] >= 2  # DO + AVOID lesson stored for the failure


def test_route_message_disabled_is_noop(isolated):
    from metano.gateway.router import MessageRouter
    route_events.set_enabled(False)
    try:
        r = _router('hello')
        asyncio.run(r.route_message('test', 'u3', '你好'))
        assert route_events.get_route_stats()['total_events'] == 0
    finally:
        route_events.set_enabled(True)
