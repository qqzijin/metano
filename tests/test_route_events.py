"""Tests for the routing feedback loop (Plan A): task signatures, bandit,
event recording, and the begin/end router hooks."""

import random

import pytest

from metano import route_events, experience


@pytest.fixture()
def evo_db(tmp_path, monkeypatch):
    """Isolate route_events + experience onto a throwaway evo.db."""
    monkeypatch.setattr(route_events, 'DB_PATH', tmp_path / 'evo.db')
    monkeypatch.setattr(experience, 'DB_PATH', tmp_path / 'evo.db')
    route_events.init_db()
    route_events.set_enabled(True)
    return tmp_path / 'evo.db'


# ── task signature / classification ────────────────────────────────────────

def test_task_signature_deterministic_and_order_independent():
    a = route_events.make_task_signature('写一个 python 冒泡排序函数')
    b = route_events.make_task_signature('写一个 python 冒泡排序函数')
    # reordering whitespace-separated tokens (same token boundaries) normalizes to the same hash
    c = route_events.make_task_signature('python 冒泡排序函数 写一个')
    assert a == b
    assert a == c
    assert a.startswith('code:')


def test_task_signature_differs_for_diff_tasks():
    assert route_events.make_task_signature('什么是机器学习') != \
        route_events.make_task_signature('帮我修复这个报错')


def test_classify_task_type():
    assert route_events.classify_task_type('帮我debug这个报错') == 'code'
    assert route_events.classify_task_type('what is the capital of france?') == 'qa'
    assert route_events.classify_task_type('写一封英文邮件') == 'chat'
    assert route_events.classify_task_type('定时每天9点执行') == 'cron'
    assert route_events.classify_task_type('') == 'chat'
    assert route_events.classify_task_type('帮我搜索研究一下市场报告') == 'research'


# ── reward ─────────────────────────────────────────────────────────────────

def test_compute_reward():
    r = route_events.compute_reward(1.0, cost_usd=0.01, latency_s=10,
                                    alpha=1.0, beta=0.01)
    assert r == pytest.approx(1.0 - 0.01 - 0.1)
    r2 = route_events.compute_reward(0.0, cost_usd=0.5, latency_s=30)
    assert r2 < 0  # latency/cost penalty can drive reward negative


# ── event recording + bandit stats ─────────────────────────────────────────

def test_record_event_and_outcome_success(evo_db):
    eid = route_events.record_event('code:abc', 'code', 'default')
    assert eid >= 1
    res = route_events.record_outcome(eid, 'success', latency_ms=1000, cost=0.005,
                                      usage={'input_tokens': 100, 'output_tokens': 50})
    assert res['status'] == 'recorded'
    assert res['outcome'] == 'success'
    stats = route_events.get_route_stats()
    assert stats['total_events'] == 1
    assert stats['by_outcome'].get('success') == 1
    assert stats['bandit'][0]['task_type'] == 'code'
    assert stats['bandit'][0]['strategy'] == 'default'
    assert stats['bandit'][0]['n'] == 1
    assert stats['bandit'][0]['wins'] == 1


def test_record_outcome_unknown_event(evo_db):
    res = route_events.record_outcome(9999, 'success')
    assert res['status'] == 'not_found'


def test_record_outcome_invalid(evo_db):
    eid = route_events.record_event('code:x', 'code', 'default')
    with pytest.raises(ValueError):
        route_events.record_outcome(eid, 'weird')


def test_failure_creates_experiences(evo_db):
    eid = route_events.record_event('code:abc', 'code', 'default')
    route_events.record_outcome(eid, 'failure', error_class='timeout',
                                response='Response timed out.')
    exp = experience.get_experience_stats()
    assert exp['total'] == 2  # one DO + one AVOID
    assert exp['by_type'].get('code', 0) == 2


# ── bandit selection ───────────────────────────────────────────────────────

def test_select_strategy_single_pool(evo_db, monkeypatch):
    monkeypatch.setattr(route_events, '_strategy_pool', lambda: ['default'])
    assert route_events.select_strategy('code') == 'default'


def test_select_strategy_cold_start_explores(evo_db, monkeypatch):
    monkeypatch.setattr(route_events, '_strategy_pool', lambda: ['provider_a', 'provider_b'])
    route_events.COLD_START_MIN = 5
    for _ in range(20):
        assert route_events.select_strategy('code') in ('provider_a', 'provider_b')


class _FakeRandom:
    """Deterministic random for bandit tests: choice always returns ``choice``,
    random() always returns ``value``."""
    def __init__(self, choice, value):
        self._choice, self._value = choice, value

    def choice(self, seq):
        return self._choice

    def random(self):
        return self._value


def test_select_strategy_exploits_best(evo_db, monkeypatch):
    monkeypatch.setattr(route_events, '_strategy_pool', lambda: ['a', 'b'])
    for i in range(6):
        eid = route_events.record_event(f'code:siga{i}', 'code', 'a')
        route_events.record_outcome(eid, 'success', latency_ms=100, cost=0.001)
    for i in range(6):
        eid = route_events.record_event(f'code:sigb{i}', 'code', 'b')
        route_events.record_outcome(eid, 'failure', latency_ms=1000, cost=0.005)
    # n=12 → eps = max(EPS_MIN, 1-12/50); patch random so we never explore.
    monkeypatch.setattr(route_events, 'EPS_MIN', 0.05)
    monkeypatch.setattr(route_events, 'random', _FakeRandom(choice='b', value=0.99))
    picks = [route_events.select_strategy('code') for _ in range(30)]
    assert all(p == 'a' for p in picks)  # 'a' has strictly better mean reward


def test_select_strategy_explores_when_random_low(evo_db, monkeypatch):
    monkeypatch.setattr(route_events, '_strategy_pool', lambda: ['a', 'b'])
    for i in range(6):
        eid = route_events.record_event(f'code:siga{i}', 'code', 'a')
        route_events.record_outcome(eid, 'success', latency_ms=100, cost=0.001)
    monkeypatch.setattr(route_events, 'EPS_MIN', 0.99)  # always explore
    monkeypatch.setattr(route_events, 'random', _FakeRandom(choice='b', value=0.1))
    assert route_events.select_strategy('code') == 'b'


# ── router hooks ───────────────────────────────────────────────────────────

def test_begin_end_route(evo_db):
    ctx = route_events.begin_route('帮我写一个python函数', '原始 prompt',
                                   'web', 'u1', 'sess1')
    assert ctx is not None
    assert ctx['strategy'] in route_events._strategy_pool()
    assert ctx['task_type'] == 'code'
    assert ctx['prompt'].startswith('原始 prompt')
    assert ctx['event_id'] >= 1

    res = route_events.end_route(ctx, '好的，这是函数定义...', latency_ms=2000,
                                 usage={'input_tokens': 10, 'output_tokens': 20})
    assert res['outcome'] == 'success'
    stats = route_events.get_route_stats()
    assert stats['total_events'] == 1
    assert stats['by_outcome'].get('success') == 1


def test_begin_route_disabled(evo_db):
    route_events.set_enabled(False)
    try:
        ctx = route_events.begin_route('hi', 'prompt')
        assert ctx is None
    finally:
        route_events.set_enabled(True)


def test_is_enabled_env(monkeypatch):
    route_events.set_enabled(None)
    monkeypatch.setenv('METANO_EXPERIENCE_ENABLED', '1')
    assert route_events.is_enabled() is True
    monkeypatch.setenv('METANO_EXPERIENCE_ENABLED', '0')
    assert route_events.is_enabled() is False
    monkeypatch.delenv('METANO_EXPERIENCE_ENABLED')


# ── independent outcome judgement ──────────────────────────────────────────

def test_judge_outcome_heuristic():
    assert route_events._judge_outcome('Error: something broke') == 'failure'
    assert route_events._judge_outcome('Response timed out.') == 'failure'
    assert route_events._judge_outcome('⚠️ 命令执行出错') == 'failure'
    assert route_events._judge_outcome('正常的回答内容') == 'success'
    assert route_events._judge_outcome('') == 'failure'


def test_custom_judge():
    route_events.set_judge(lambda resp: 'failure' if 'bad' in resp else 'success')
    try:
        assert route_events._judge_outcome('this is bad') == 'failure'
        assert route_events._judge_outcome('all good') == 'success'
    finally:
        route_events.set_judge(None)


def test_detect_error_class():
    assert route_events._detect_error_class('Response timed out.') == 'timeout'
    assert route_events._detect_error_class('invalid api key') == 'auth'
    assert route_events._detect_error_class('rate limit hit 429') == 'rate_limit'
    assert route_events._detect_error_class('Traceback (most recent call last)') == 'exception'
    assert route_events._detect_error_class('') == 'empty'
    assert route_events._detect_error_class('unknown problem') == 'generic'
