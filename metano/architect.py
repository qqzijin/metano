"""Architecture self-restructure engine: build self-model, detect bottlenecks, propose safe changes.

The architect can ONLY modify:
- cron/jobs.json (schedules)
- skills/ trigger patterns
- gateway_config.yaml routing priorities
- evolution_meta parameters

It can NEVER modify:
- Python source code
- Database schemas
- Auth/security config
"""
import json
import os
import time
from pathlib import Path
from .evo_models import _get_conn, get_meta, set_meta, get_recent_actions, get_action_stats, get_rules, add_rule, get_proposals, update_proposal_status
from .evolution import _log
from .llm_call import call_llm
from metano.log import logger
from .paths import home_dir, CRON_JOBS_FILE as CRON_FILE, CONFIG_PATH as GATEWAY_CONFIG, ARCH_SNAP_DIR
ANTHROPIC_MODEL = os.environ.get('HONCHO_MODEL', 'claude-sonnet-4-6')
PROJECT_DIR = home_dir()
SRC_DIR = Path(__file__).resolve().parent
MODIFIABLE_FILES = {'cron/jobs.json', 'skills/*/SKILL.md trigger', 'gateway_config.yaml'}


def _llm_provider_available() -> bool:
    """Whether a usable LLM provider exists, resolved at call time.

    M6: the old gate read a module-level ANTHROPIC_API_KEY env snapshot captured
    at import time — under the cron/daemon process that env is unset, so the
    deep-analysis LLM branch never ran. Reflect the live ModelRouter (same
    pattern as reflector._llm_provider_available).
    """
    try:
        from .model_router import model_router
        p = model_router.get_provider()
        if p and getattr(p, 'api_key', ''):
            return True
    except Exception:
        logger.exception("architect: provider resolution failed")
    return bool(os.environ.get('ANTHROPIC_API_KEY', ''))


def _call_llm(system_prompt: str, user_prompt: str, session_id: str = '') -> str:
    text, _ = call_llm(system_prompt, user_prompt, session_id=session_id)
    return text

def build_architecture_model() -> dict:
    """Build a topological model of the system: components, tools, routes, cron jobs."""
    model = {'timestamp': time.time(), 'components': [], 'mcp_tools': [], 'cron_jobs': [], 'routes': [], 'rules': []}
    src_dir = SRC_DIR
    if src_dir.exists():
        for f in src_dir.glob('*.py'):
            if f.name.startswith('_'):
                continue
            size = f.stat().st_size
            imports = []
            content = f.read_text()[:5000]
            for line in content.split('\n'):
                line = line.strip()
                if line.startswith('from .') or line.startswith('import .'):
                    imports.append(line)
            model['components'].append({'name': f.stem, 'size_bytes': size, 'imports': imports[:10]})
    try:
        from .mcp_server import mcp
        tools = mcp._tool_manager.list_tools() if hasattr(mcp, '_tool_manager') else []
        model['mcp_tools'] = [{'name': t.name, 'description': (t.description or '')[:80]} for t in tools]
    except Exception:
        logger.exception("architect: apply_restructure failed")
        model['mcp_tools'] = []
    if CRON_FILE.exists():
        data = json.loads(CRON_FILE.read_text())
        jobs = data.get('jobs', data) if isinstance(data, dict) else data
        for j in jobs:
            model['cron_jobs'].append({'id': j.get('id', ''), 'name': j.get('name', ''), 'schedule': j.get('schedule', {}), 'enabled': j.get('enabled', True), 'last_error': j.get('last_error')})
    try:
        from .web_server import app
        routes = []
        for route in app.routes:
            if hasattr(route, 'methods') and hasattr(route, 'path'):
                routes.append({'path': route.path, 'methods': list(route.methods or [])})
        model['routes'] = routes[:50]
    except Exception:
        logger.exception("architect: apply_restructure failed")
        model['routes'] = []
    rules = get_rules(active_only=True)
    model['rules'] = [{'id': r['id'], 'kind': r['kind'], 'content': r['content'][:60]} for r in rules]
    ARCH_SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snap_file = ARCH_SNAP_DIR / f'snap_{int(time.time())}.json'
    snap_file.write_text(json.dumps(model, ensure_ascii=False, indent=2))
    _log('architect', 'build_model', {'components': len(model['components']), 'tools': len(model['mcp_tools']), 'cron_jobs': len(model['cron_jobs']), 'routes': len(model['routes'])})
    return model

def detect_bottlenecks(model: dict) -> list[dict]:
    """Analyze architecture model for bottlenecks and anti-patterns."""
    findings = []
    for comp in model.get('components', []):
        if comp.get('size_bytes', 0) > 50000:
            findings.append({'type': 'oversized_module', 'component': comp['name'], 'size_kb': comp['size_bytes'] / 1024, 'severity': 'medium', 'suggestion': f"考虑将 {comp['name']}.py 拆分为多个子模块"})
    stats = get_action_stats()
    for outcome, count in stats.get('by_outcome', {}).items():
        if outcome == 'failure' and count > 5:
            total = stats.get('total', 1)
            findings.append({'type': 'high_failure_rate', 'outcome': outcome, 'count': count, 'rate': count / max(total, 1), 'severity': 'high', 'suggestion': '检查频繁失败的操作类型，调整策略或增加规则'})
    for r in get_rules(active_only=True):
        if r.get('times_applied', 0) >= 3 and r.get('effectiveness', 0) < 0.3:
            findings.append({'type': 'ineffective_rule', 'rule_id': r['id'], 'content': r['content'][:60], 'effectiveness': r['effectiveness'], 'severity': 'medium', 'suggestion': f"规则 '{r['content'][:40]}' effectiveness 过低，考虑禁用或修改"})
    for job in model.get('cron_jobs', []):
        if job.get('last_error'):
            findings.append({'type': 'cron_error', 'job_id': job['id'], 'error': job['last_error'][:100], 'severity': 'high', 'suggestion': f"修复 cron job {job['id']} 的错误或暂时禁用"})
    disabled = [j for j in model.get('cron_jobs', []) if not j.get('enabled', True)]
    if len(disabled) > 2:
        findings.append({'type': 'many_disabled_crons', 'count': len(disabled), 'severity': 'low', 'suggestion': f'有 {len(disabled)} 个 cron jobs 被禁用，检查是否需要重新启用或清理'})
    if len(findings) >= 2 and _llm_provider_available():
        llm_findings = _llm_analyze_architecture(model, findings)
        findings.extend(llm_findings)
    _log('architect', 'detect_bottlenecks', {'findings': len(findings)})
    return findings

def _llm_analyze_architecture(model: dict, initial_findings: list[dict]) -> list[dict]:
    """Use LLM for deeper architectural analysis."""
    system = 'You are an architecture analyst for an AI agent system. Given the current architecture model and initial bottleneck findings,\nidentify deeper architectural issues that may not be obvious from simple metrics.\n\nReturn a JSON array of additional findings:\n[{"type": "architecture_issue", "description": "...", "severity": "high/medium/low", "suggestion": "concrete change proposal", "modifiable_target": "cron/skills/gateway_config/meta"}]\n\nCRITICAL: You can ONLY suggest changes to:\n- cron/jobs.json (schedules, enable/disable)\n- skills trigger patterns\n- gateway_config.yaml routing\n- evolution_meta parameters\n\nNEVER suggest changes to Python source, DB schemas, or auth config.'
    model_summary = {'components': [{'name': c['name'], 'size_kb': c.get('size_bytes', 0) / 1024} for c in model.get('components', [])], 'tools_count': len(model.get('mcp_tools', [])), 'cron_jobs': [{'id': j['id'], 'enabled': j.get('enabled')} for j in model.get('cron_jobs', [])], 'routes_count': len(model.get('routes', [])), 'rules_count': len(model.get('rules', []))}
    prompt = f'Architecture:\n{json.dumps(model_summary, ensure_ascii=False)}\n\nInitial findings:\n{json.dumps(initial_findings[:5], ensure_ascii=False)}'
    try:
        response = _call_llm(system, prompt)
        if '[' in response and ']' in response:
            start = response.index('[')
            end = response.rindex(']') + 1
            return json.loads(response[start:end])
    except (json.JSONDecodeError, ValueError):
        pass
    return []

def propose_restructure(findings: list[dict]) -> list[dict]:
    """Generate concrete proposals from bottleneck findings.

    Only creates proposals that modify whitelisted targets.
    Each proposal requires explicit approval before execution.
    """
    proposals = []
    for f in findings:
        target = f.get('modifiable_target', '')
        severity = f.get('severity', 'low')
        suggestion = f.get('suggestion', '')
        if not target or not suggestion:
            continue
        if target not in ('cron', 'skills', 'gateway_config', 'meta'):
            continue
        if severity not in ('high', 'medium'):
            continue
        proposal = {'id': f"arch-{int(time.time())}-{f['type'][:8]}", 'type': 'architecture_restructure', 'target': target, 'description': suggestion, 'finding': f['type'], 'severity': severity, 'status': 'pending', 'created_at': time.time()}
        proposals.append(proposal)
    _log('architect', 'propose', {'proposals': len(proposals)})
    return proposals

def apply_restructure(proposal_id: int) -> dict | None:
    """Apply an approved architecture restructure proposal.

    Executes the actual change within safety constraints.
    """
    from .evo_models import get_proposals, update_proposal_status
    proposals = get_proposals(status='approved')
    target_proposal = None
    for p in proposals:
        if p['id'] == proposal_id:
            target_proposal = p
            break
    if not target_proposal:
        return None
    detail = json.loads(target_proposal.get('detail', '{}')) if isinstance(target_proposal.get('detail'), str) else target_proposal.get('detail', {})
    target = detail.get('target', '')
    description = target_proposal.get('content', '')
    try:
        if target == 'cron':
            result = _apply_cron_change(description)
        elif target == 'meta':
            result = _apply_meta_change(description)
        elif target == 'skills':
            result = _apply_skills_change(description)
        elif target == 'gateway_config':
            result = _apply_gateway_change(description)
        else:
            return {'status': 'rejected', 'reason': f'Unknown target: {target}'}
        update_proposal_status(proposal_id, 'applied')
        _log('architect', 'apply', {'proposal_id': proposal_id, 'target': target})
        return {'status': 'applied', 'proposal_id': proposal_id, 'result': result}
    except Exception as e:
        logger.exception("architect: apply_restructure failed")
        return {'status': 'error', 'proposal_id': proposal_id, 'error': str(e)}

def _apply_cron_change(description: str) -> dict:
    """Apply a change to cron/jobs.json."""
    if not CRON_FILE.exists():
        return {'status': 'no_cron_file'}
    jobs = json.loads(CRON_FILE.read_text())
    for job in jobs:
        job_id = job.get('id', '')
        desc_lower = description.lower()
        if any((kw in job_id for kw in desc_lower.split())):
            job['enabled'] = 'disable' not in desc_lower
            # Persist through the atomic, flock-serialized store writer instead
            # of a raw write_text so a concurrent daemon/Web/MCP write can never
            # tear jobs.json (P1-3).
            from .cron_daemon import save_jobs
            save_jobs(jobs)
            return {'status': 'toggled', 'job_id': job_id}
    return {'status': 'no_matching_job'}

def _apply_meta_change(description: str) -> dict:
    """Apply a change to evolution_meta parameters."""
    for word in description.split():
        if '=' in word:
            key, _, value = word.partition('=')
            try:
                value = float(value)
                set_meta(key, value)
                return {'status': 'set_meta', 'key': key, 'value': value}
            except ValueError:
                set_meta(key, value)
                return {'status': 'set_meta', 'key': key, 'value': value}
    return {'status': 'no_change_detected'}

def _apply_skills_change(description: str) -> dict:
    """Apply a change to skills trigger patterns (informational only).

    NOTE: Skills changes cannot be automated safely. Manual review required:
    - Edit skills trigger patterns in skills_data/*/SKILL.md
    """
    logger.warning("architect: skills change requires manual review — no automated apply")
    _log('architect', 'skills_change_skipped',
         {'reason': 'requires_manual_review', 'description': description[:200]})
    return {'status': 'skills_change_requires_manual_review', 'description': description}

def _apply_gateway_change(description: str) -> dict:
    """Apply a change to gateway_config.yaml (informational only).

    NOTE: Gateway config changes cannot be automated safely. Manual review required:
    - Edit gateway_config.yaml directly
    - Restart gateway via ./metano.sh restart
    """
    logger.warning("architect: gateway change requires manual review — no automated apply")
    _log('architect', 'gateway_change_skipped',
         {'reason': 'requires_manual_review', 'description': description[:200]})
    return {'status': 'gateway_change_requires_manual_review', 'description': description}