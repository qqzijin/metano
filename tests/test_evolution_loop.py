"""Tests for the evolution system closed loop: observe → reason → act → reflect."""
import time
import pytest
from metano.evo_models import (
    add_proposal, get_proposals, update_proposal_status,
    log_action, add_rule, rule_count,
)


class TestProposalCRUD:
    def test_add_and_get_proposal(self):
        pid = add_proposal('behavior_improvement', 'Always verify before claiming done', source='test')
        assert pid > 0
        proposals = get_proposals(status='pending')
        assert any(p['id'] == pid for p in proposals)

    def test_update_proposal_status_approved(self):
        pid = add_proposal('config_change', 'timeout=60', source='test')
        update_proposal_status(pid, 'approved')
        proposals = get_proposals(status='approved')
        assert any(p['id'] == pid for p in proposals)

    def test_update_proposal_status_rejected(self):
        pid = add_proposal('rule_add', 'block rm -rf', source='test')
        update_proposal_status(pid, 'rejected')
        proposals = get_proposals(status='rejected')
        assert any(p['id'] == pid for p in proposals)

    def test_filter_by_type(self):
        pid = add_proposal('claude_md_inject', 'test injection', source='test')
        proposals = get_proposals(proposal_type='claude_md_inject')
        assert any(p['id'] == pid for p in proposals)

    def test_update_applied_with_result(self):
        pid = add_proposal('behavior_improvement', 'test apply', source='test')
        update_proposal_status(pid, 'approved')
        update_proposal_status(pid, 'applied', 'successfully applied')
        proposals = get_proposals(status='applied')
        target = next(p for p in proposals if p['id'] == pid)
        assert target['result'] == 'successfully applied'


class TestProposalApproval:
    def test_process_approval_reply(self):
        from metano.evolution_notify import process_approval_reply
        pid = add_proposal('behavior_improvement', 'reply test', source='test')
        result = process_approval_reply(f'批准#{pid}')
        assert result is not None
        assert result['action'] == 'approved'
        assert result['proposal_id'] == pid

    def test_process_rejection_reply(self):
        from metano.evolution_notify import process_approval_reply
        pid = add_proposal('behavior_improvement', 'reject test', source='test')
        result = process_approval_reply(f'拒绝#{pid}')
        assert result is not None
        assert result['action'] == 'rejected'
        assert result['proposal_id'] == pid

    def test_non_approval_reply_ignored(self):
        from metano.evolution_notify import process_approval_reply
        result = process_approval_reply('今天天气不错')
        assert result is None


class TestCronSchedule:
    def test_load_jobs(self):
        from metano.cron_daemon import load_jobs
        jobs = load_jobs()
        assert len(jobs) > 0
        actions = [j.get('action', '') for j in jobs]
        assert 'evolution.harvest' in actions
        assert 'evolution.reflect' in actions
        assert 'evolution.adapt' in actions
        assert 'evolution.evaluate' in actions

    def test_compute_next_run_plain_string(self):
        from metano.cron_daemon import compute_next_run
        result = compute_next_run('0 */6 * * *', None)
        assert result is not None

    def test_compute_next_run_dict(self):
        from metano.cron_daemon import compute_next_run
        result = compute_next_run({'kind': 'cron', 'expr': '0 */6 * * *'}, None)
        assert result is not None

    def test_compute_next_run_interval(self):
        from metano.cron_daemon import compute_next_run
        result = compute_next_run({'kind': 'interval', 'expr': '30'}, None)
        assert result is not None


class TestEffectEvaluation:
    def test_record_baseline(self):
        from metano.evolution_eval import record_baseline
        pid = add_proposal('behavior_improvement', 'eval test', source='test')
        baseline = record_baseline(pid)
        assert baseline['proposal_id'] == pid
        assert 'total_actions_24h' in baseline
        assert 'avg_rule_effectiveness' in baseline

    def test_evaluate_too_early(self):
        from metano.evolution_eval import record_baseline, evaluate_effect
        pid = add_proposal('behavior_improvement', 'early eval test', source='test')
        update_proposal_status(pid, 'approved')
        update_proposal_status(pid, 'applied', 'test')
        record_baseline(pid)
        result = evaluate_effect(pid)
        assert result is not None
        assert result['status'] == 'too_early'


class TestCronActionRegistry:
    def test_actions_registered(self):
        from metano.cron_daemon import _register_default_actions, ACTIONS
        _register_default_actions()
        expected = [
            'evolution.harvest', 'evolution.reflect', 'evolution.adapt',
            'evolution.maintain', 'evolution.explore', 'evolution.architect',
            'evolution.introspect', 'evolution.evaluate',
        ]
        for action in expected:
            assert action in ACTIONS, f'{action} not registered'
