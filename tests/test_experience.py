"""Tests for the experience memory (Plan A): reflections, retrieval,
DO:/AVOID: prompt injection, and anti-degradation cleanup."""

import time

import pytest

from metano import experience


@pytest.fixture()
def exp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(experience, 'DB_PATH', tmp_path / 'evo.db')
    experience.init_db()
    return tmp_path / 'evo.db'


def _insert(conn, task_type, direction, summary, effectiveness=0.5,
            active=1, created_at=None):
    conn.execute(
        'INSERT INTO route_experiences '
        '(task_type, task_signature, direction, summary, detail, outcome, '
        'source_event_id, effectiveness, active, created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (task_type, '', direction, summary, '', '', 0, effectiveness, active,
         created_at if created_at is not None else time.time()),
    )


# ── reflection ─────────────────────────────────────────────────────────────

def test_record_reflection_creates_do_and_avoid(exp_db):
    res = experience.record_reflection('code', 'code:sig', error_class='timeout',
                                       response='Response timed out.')
    assert res['status'] == 'ok'
    assert res['created'] == 2  # one DO + one AVOID
    stats = experience.get_experience_stats()
    assert stats['total'] == 2
    assert stats['by_type'].get('code', 0) == 2


def test_record_reflection_dedupes_same_lesson(exp_db):
    for _ in range(3):
        experience.record_reflection('code', 'code:sig', error_class='timeout',
                                     response='Response timed out.')
    stats = experience.get_experience_stats()
    assert stats['total'] == 2  # deduped, not 6
    # reinforcement bumped effectiveness above the base 0.5
    conn = experience._get_conn()
    effs = [r['effectiveness'] for r in
            conn.execute('SELECT effectiveness FROM route_experiences').fetchall()]
    conn.close()
    assert all(e > 0.5 for e in effs)


def test_record_reflection_skip_without_task_type(exp_db):
    assert experience.record_reflection('', error_class='timeout')['status'] == 'skip'


# ── retrieval + injection ──────────────────────────────────────────────────

def test_retrieve_relevant_first(exp_db):
    conn = experience._get_conn()
    _insert(conn, 'code', 'do', 'code lesson about sorting')
    _insert(conn, 'chat', 'do', 'chat lesson about greetings')
    conn.commit()
    conn.close()
    items = experience.retrieve_experiences('sorting', 'code', limit=5)
    assert items
    assert items[0]['task_type'] == 'code'


def test_inactive_experience_not_retrieved(exp_db):
    conn = experience._get_conn()
    _insert(conn, 'code', 'do', 'active lesson', active=1)
    _insert(conn, 'code', 'do', 'inactive lesson', active=0)
    conn.commit()
    conn.close()
    summaries = [i['summary'] for i in experience.retrieve_experiences('lesson', 'code')]
    assert 'active lesson' in summaries
    assert 'inactive lesson' not in summaries


def test_irrelevant_task_type_filtered(exp_db):
    conn = experience._get_conn()
    _insert(conn, 'code', 'do', 'sorting code lesson')
    conn.commit()
    conn.close()
    # chat query with no keyword overlap should not match a code-only store
    items = experience.retrieve_experiences('你好', 'chat', limit=5)
    assert items == []


def test_inject_experiences_formats_do_avoid(exp_db):
    experience.record_reflection('code', 'code:sig', error_class='timeout',
                                 response='Response timed out.')
    prompt = '用户问题：写冒泡排序'
    new_prompt, n = experience.inject_experiences(prompt, '写冒泡排序', 'code')
    assert n == 2
    assert new_prompt.startswith(prompt)
    assert 'DO:' in new_prompt
    assert 'AVOID:' in new_prompt
    assert '经验参考' in new_prompt


def test_inject_experiences_noop_when_no_match(exp_db):
    prompt = 'hi'
    new_prompt, n = experience.inject_experiences(prompt, 'nothing relevant', 'chat')
    assert n == 0
    assert new_prompt == prompt


# ── anti-degradation ───────────────────────────────────────────────────────

def test_reward_relevant_decays_avoid_reinforces_do(exp_db):
    experience.record_reflection('code', '', error_class='timeout', response='x')
    conn = experience._get_conn()
    before = {r['direction']: r['effectiveness']
              for r in conn.execute('SELECT direction, effectiveness FROM route_experiences').fetchall()}
    conn.close()
    experience.reward_relevant('code', 'success')
    conn = experience._get_conn()
    after = {r['direction']: r['effectiveness']
             for r in conn.execute('SELECT direction, effectiveness FROM route_experiences').fetchall()}
    conn.close()
    assert after['avoid'] < before['avoid']
    assert after['do'] > before['do']


def test_reward_relevant_failure_boosts_all(exp_db):
    experience.record_reflection('code', '', error_class='timeout', response='x')
    conn = experience._get_conn()
    before = {r['direction']: r['effectiveness']
              for r in conn.execute('SELECT direction, effectiveness FROM route_experiences').fetchall()}
    conn.close()
    experience.reward_relevant('code', 'failure')
    conn = experience._get_conn()
    after = {r['direction']: r['effectiveness']
             for r in conn.execute('SELECT direction, effectiveness FROM route_experiences').fetchall()}
    conn.close()
    assert after['avoid'] > before['avoid']
    assert after['do'] > before['do']


def test_cleanup_caps_per_task_type(exp_db):
    conn = experience._get_conn()
    for i in range(12):
        _insert(conn, 'code', 'do', f'code lesson {i}')
    for i in range(3):
        _insert(conn, 'chat', 'do', f'chat lesson {i}')
    conn.commit()
    conn.close()
    res = experience.cleanup_experiences(keep_per_type=10)
    assert res['status'] == 'ok'
    stats = experience.get_experience_stats()
    assert stats['by_type']['code'] == 10
    assert stats['by_type']['chat'] == 3


def test_cleanup_removes_inactive_and_weak(exp_db):
    conn = experience._get_conn()
    _insert(conn, 'code', 'do', 'inactive old', effectiveness=0.1, active=0)
    _insert(conn, 'code', 'do', 'weak old', effectiveness=0.1,
            created_at=time.time() - 40 * 86400)
    _insert(conn, 'code', 'do', 'keep me', effectiveness=0.8)
    conn.commit()
    conn.close()
    res = experience.cleanup_experiences(keep_per_type=10)
    assert res['deleted_inactive'] == 1
    assert res['deleted_low'] == 1
    stats = experience.get_experience_stats()
    assert stats['total'] == 1
