"""Tests for the Be-ACTIVE immediate learning + skill protection features."""
import json
import shutil

from metano.skills.manager import SkillManager
from metano.skill_improvement import (
    apply_skill_improvement,
    find_relevant_skills,
    propose_skill_improvements,
)
from metano.evo_models import add_proposal, update_proposal_status, get_proposals
from metano.adapter import apply_proposal


def _cleanup_skill(name):
    m = SkillManager()
    rec = m.loader.find_by_name(name)
    if rec:
        shutil.rmtree(rec.path.parent)
        m.loader.discover_all(force=True)


class TestSkillProtection:
    def test_bundled_skill_is_protected(self):
        m = SkillManager()
        assert m.is_protected('code-review')
        assert m.is_protected('debug')

    def test_bundled_edit_patch_delete_refused(self):
        m = SkillManager()
        rec = m.loader.find_by_name('debug')
        real_body = rec.body[:50]
        for op in (
            lambda: m.patch('debug', real_body, 'x'),
            lambda: m.edit('debug', 'new body'),
            lambda: m.delete('debug'),
        ):
            res = op()
            assert 'error' in res, f'expected protection error, got {res}'

    def test_user_skill_not_protected_and_editable(self):
        m = SkillManager()
        m.create('tmp-prot-test', 'development', 'tmp', 'body')
        try:
            assert not m.is_protected('tmp-prot-test')
            assert m.edit('tmp-prot-test', 'new body')['status'] == 'edited'
            assert m.patch('tmp-prot-test', 'new body', 'patched')['status'] == 'edited'
            assert m.delete('tmp-prot-test')['status'] == 'deleted'
        finally:
            _cleanup_skill('tmp-prot-test')


class TestSkillImprovement:
    def test_find_relevant_skills(self):
        # '后端API字段' maps to code-review domain
        skills = find_relevant_skills('你改了后端API字段但没同步前端，导致不匹配')
        names = [s.name for s in skills]
        assert any('code-review' in n for n in names), names

    def test_propose_creates_deduped_proposals(self):
        corrections = [{'content': '改了后端API必须同步前端，否则字段不匹配'}]
        r1 = propose_skill_improvements(corrections, source='test')
        r2 = propose_skill_improvements(corrections, source='test')
        assert r1['proposals_created'] >= 1
        assert r2['proposals_created'] == 0, 'should dedupe against existing proposals'
        # cleanup test proposals
        conn = __import__('metano.evo_models', fromlist=['_get_conn'])._get_conn()
        conn.execute("DELETE FROM proposals WHERE source='test'")
        conn.commit()
        conn.close()

    def test_apply_creates_references_file(self):
        m = SkillManager()
        m.create('tmp-apply-test', 'development', 'tmp', 'body\n## Pitfalls\n- none')
        try:
            detail = json.dumps({'skill': 'tmp-apply-test', 'correction': '必须验证'}, ensure_ascii=False)
            res = apply_skill_improvement('为技能补充经验', detail)
            assert res['status'] == 'applied'
            rec = m.loader.find_by_name('tmp-apply-test')
            skill_dir = rec.path.parent
            refs = skill_dir / 'references'
            assert refs.exists() and list(refs.glob('*.md'))
            assert 'references/' in (skill_dir / 'SKILL.md').read_text()
        finally:
            _cleanup_skill('tmp-apply-test')


class TestApprovalFlow:
    def test_approved_skill_proposal_applies(self):
        m = SkillManager()
        m.create('tmp-approve-test', 'development', 'tmp', 'body\n## Pitfalls\n- none')
        try:
            detail = json.dumps({'skill': 'tmp-approve-test', 'correction': '经验'}, ensure_ascii=False)
            pid = add_proposal('skill_improvement', '补充经验', detail, source='test')
            update_proposal_status(pid, 'approved')
            res = apply_proposal(pid)
            assert res['status'] == 'applied'
        finally:
            _cleanup_skill('tmp-approve-test')
