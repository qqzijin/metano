"""Effect evaluation for evolution proposals: measure impact after application.

Records baseline metrics before a proposal is applied, then compares
after a configurable evaluation window (default 24h). Results feed back
as observations into the evolution system.
"""

import json
import time
from metano.log import logger
from metano.evo_models import _get_conn
from metano.code_introspector import AGENT_USER_ID


def record_baseline(proposal_id: int) -> dict:
    """Snapshot current system metrics before applying a proposal.

    Metrics captured:
    - action_log success/failure rate (last 24h)
    - silent exception count from introspector
    - pending proposal count
    """
    conn = _get_conn()
    now = time.time()
    since = now - 86400  # 24h ago

    # Action log stats
    try:
        total_actions = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE timestamp > ?", (since,)
        ).fetchone()[0]
        action_types = conn.execute(
            "SELECT COUNT(DISTINCT action_type) FROM action_log WHERE timestamp > ?", (since,)
        ).fetchone()[0]
        success_rate = conn.execute(
            "SELECT CASE WHEN COUNT(*) > 0 THEN 1.0 * SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) / COUNT(*) ELSE 0 END FROM action_log WHERE timestamp > ?",
            (since,)
        ).fetchone()[0]
    except Exception:
        total_actions = 0
        action_types = 0
        success_rate = 0.0

    # Rule effectiveness
    try:
        avg_effectiveness = conn.execute(
            "SELECT AVG(effectiveness) FROM agent_rules WHERE active = 1"
        ).fetchone()[0] or 0.0
        active_rule_count = conn.execute(
            "SELECT COUNT(*) FROM agent_rules WHERE active = 1"
        ).fetchone()[0]
    except Exception:
        avg_effectiveness = 0.0
        active_rule_count = 0

    # Pending proposals
    try:
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE status = 'pending'"
        ).fetchone()[0]
    except Exception:
        pending_count = 0

    baseline = {
        'proposal_id': proposal_id,
        'timestamp': now,
        'total_actions_24h': total_actions,
        'distinct_action_types': action_types,
        'success_rate_24h': success_rate,
        'avg_rule_effectiveness': avg_effectiveness,
        'active_rule_count': active_rule_count,
        'pending_proposals': pending_count,
    }

    # Store baseline
    conn.execute(
        "INSERT INTO effect_baselines (proposal_id, baseline_json, created_at) VALUES (?, ?, ?)",
        (proposal_id, json.dumps(baseline), now),
    )
    conn.commit()
    conn.close()
    return baseline


def evaluate_effect(proposal_id: int) -> dict | None:
    """Compare current metrics against baseline for a proposal.

    Returns a dict with before/after metrics and assessment, or None if
    no baseline exists or not enough time has passed.
    """
    conn = _get_conn()

    row = conn.execute(
        "SELECT baseline_json, created_at FROM effect_baselines WHERE proposal_id = ? ORDER BY created_at DESC LIMIT 1",
        (proposal_id,),
    ).fetchone()
    if not row:
        conn.close()
        return None

    baseline = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    baseline_time = row[1]

    # Require at least 1 hour since application
    if time.time() - baseline_time < 3600:
        conn.close()
        return {'status': 'too_early', 'hours_elapsed': (time.time() - baseline_time) / 3600}

    # Current metrics (same as baseline logic)
    now = time.time()
    since = now - 86400
    try:
        total_actions = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE timestamp > ?", (since,)
        ).fetchone()[0]
        action_types = conn.execute(
            "SELECT COUNT(DISTINCT action_type) FROM action_log WHERE timestamp > ?", (since,)
        ).fetchone()[0]
        success_rate = conn.execute(
            "SELECT CASE WHEN COUNT(*) > 0 THEN 1.0 * SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) / COUNT(*) ELSE 0 END FROM action_log WHERE timestamp > ?",
            (since,)
        ).fetchone()[0]
    except Exception:
        total_actions = 0
        action_types = 0
        success_rate = 0.0

    try:
        avg_effectiveness = conn.execute(
            "SELECT AVG(effectiveness) FROM agent_rules WHERE active = 1"
        ).fetchone()[0] or 0.0
    except Exception:
        avg_effectiveness = 0.0

    current = {
        'total_actions_24h': total_actions,
        'distinct_action_types': action_types,
        'success_rate_24h': success_rate,
        'avg_rule_effectiveness': avg_effectiveness,
    }

    # Assess improvement
    eff_delta = current.get('success_rate_24h', 0) - baseline.get('success_rate_24h', 0)
    rule_eff_delta = current.get('avg_rule_effectiveness', 0) - baseline.get('avg_rule_effectiveness', 0)
    assessment = 'neutral'
    if eff_delta > 0.05 or rule_eff_delta > 0.05:
        assessment = 'positive'
    elif eff_delta < -0.05 or rule_eff_delta < -0.05:
        assessment = 'negative'

    result = {
        'proposal_id': proposal_id,
        'status': 'evaluated',
        'baseline': baseline,
        'current': current,
        'success_rate_delta': round(eff_delta, 3),
        'rule_effectiveness_delta': round(rule_eff_delta, 3),
        'assessment': assessment,
    }

    # Write evaluation as observation for the evolution system
    try:
        from metano.honcho.models import get_honcho_db, add_observation, get_user, create_user
        hconn = get_honcho_db()
        user = get_user(hconn, AGENT_USER_ID)
        if not user:
            create_user(hconn, AGENT_USER_ID)
        obs_text = (
            f"[effect_eval:{assessment}] Proposal #{proposal_id} — "
            f"success_rate: {baseline.get('success_rate_24h', 0):.0%}→{current['success_rate_24h']:.0%} "
            f"(delta={eff_delta:+.0%}), actions: {baseline.get('total_actions_24h', 0)}→{current['total_actions_24h']}"
        )
        add_observation(hconn, AGENT_USER_ID, obs_text, category='effect_evaluation')
    except Exception:
        logger.exception("effect_eval: failed to write observation")

    conn.close()
    return result


def evaluate_all_recent() -> list[dict]:
    """Evaluate all proposals applied in the last 48 hours."""
    conn = _get_conn()
    since = time.time() - 172800  # 48h
    rows = conn.execute(
        "SELECT id FROM proposals WHERE status = 'applied' AND applied_at > ?",
        (since,),
    ).fetchall()
    conn.close()

    results = []
    for row in rows:
        r = evaluate_effect(row[0])
        if r:
            results.append(r)
    return results
