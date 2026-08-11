"""Evolution orchestrator: coordinates Observe → Reason → Act → Reflect cycle."""
import json
import os
import time
from pathlib import Path
from .harvester import harvest_recent_sessions
from .adapter import execute_adaptation_cycle, load_suggestions, approve_suggestion, reject_suggestion
from .reflector import reflect_on_model
from .honcho.models import init_honcho_db, get_honcho_db, get_profile, get_user, create_user, get_beliefs, decay_beliefs, archive_old_contradictions, belief_stage
from .honcho.dialectic import compress_beliefs
from .db import init_db
from .evo_models import init_db as init_evo_db, get_rules as get_agent_rules, rule_count, get_action_stats, migrate_from_honcho, EVO_DB_PATH, add_audit, get_audit, get_daily_cost
from metano.log import logger
from .paths import EVOLUTION_DIR, PAUSE_FLAG, COST_CONFIG_FLAG, LOG_FILE

# Cost circuit breaker thresholds (USD)
# 基于网络探索发现：进化系统因成本过高($487.97)被暂停
# 调整阈值：提高暂停阈值以避免频繁暂停，增加自动恢复机制
DEFAULT_COST_WARN = 10.0
DEFAULT_COST_PAUSE = 50.0
DEFAULT_COST_STOP = 100.0


def _load_cost_config() -> dict:
    """Load cost circuit breaker config from file."""
    if COST_CONFIG_FLAG.exists():
        try:
            return json.loads(COST_CONFIG_FLAG.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        'warn_threshold': DEFAULT_COST_WARN,
        'pause_threshold': DEFAULT_COST_PAUSE,
        'stop_threshold': DEFAULT_COST_STOP,
        'auto_resume_hours': 24,
    }


def _save_cost_config(config: dict):
    """Save cost circuit breaker config to file."""
    EVOLUTION_DIR.mkdir(parents=True, exist_ok=True)
    COST_CONFIG_FLAG.write_text(json.dumps(config, ensure_ascii=False, indent=2))


def _get_circuit_state() -> dict:
    """Compute current circuit breaker state from cost vs thresholds."""
    config = _load_cost_config()
    cost = _estimate_daily_cost()
    state = 'normal'
    if cost >= config.get('stop_threshold', DEFAULT_COST_STOP):
        state = 'stopped'
    elif cost >= config.get('pause_threshold', DEFAULT_COST_PAUSE):
        state = 'paused'
    elif cost >= config.get('warn_threshold', DEFAULT_COST_WARN):
        state = 'warning'
    return {
        'state': state,
        'daily_cost': round(cost, 4),
        'warn_threshold': config.get('warn_threshold', DEFAULT_COST_WARN),
        'pause_threshold': config.get('pause_threshold', DEFAULT_COST_PAUSE),
        'stop_threshold': config.get('stop_threshold', DEFAULT_COST_STOP),
        'auto_resume_hours': config.get('auto_resume_hours', 24),
    }


def _check_cost_circuit() -> dict:
    """Evaluate cost circuit breaker and take action if needed.

    Returns the circuit state dict.  Side-effects:
    - pauses evolution if threshold crossed
    - auto-resumes if auto_resume_hours have elapsed since pause
    - logs the event
    """
    circuit = _get_circuit_state()
    state = circuit['state']
    if state == 'stopped' and not _is_paused():
        evolution_pause()
        _log('maintain', 'cost_stop', circuit)
    elif state == 'paused' and not _is_paused():
        evolution_pause()
        _log('maintain', 'cost_pause', circuit)
    elif state == 'warning':
        _log('maintain', 'cost_warn', circuit)
    # Auto-resume: if paused by cost, check if enough time has elapsed
    if _is_paused() and PAUSE_FLAG.exists():
        try:
            pause_time = PAUSE_FLAG.stat().st_mtime
            elapsed_hours = (time.time() - pause_time) / 3600
            auto_resume = circuit.get('auto_resume_hours', 24)
            if elapsed_hours >= auto_resume:
                current_cost = circuit['daily_cost']
                if current_cost < circuit.get('pause_threshold', DEFAULT_COST_PAUSE):
                    evolution_resume()
                    _log('maintain', 'auto_resume', {'elapsed_hours': round(elapsed_hours, 1), 'current_cost': current_cost})
        except OSError:
            pass
    return circuit


def _is_paused() -> bool:
    return PAUSE_FLAG.exists()

def _log(phase: str, action: str, detail: dict = None, cost: float = 0, model: str = '', session_id: str = ''):
    detail_str = json.dumps(detail, ensure_ascii=False) if detail else ''
    add_audit(phase, action, detail_str, cost=cost, model=model, session_id=session_id)
    EVOLUTION_DIR.mkdir(parents=True, exist_ok=True)
    entry = {'timestamp': time.time(), 'phase': phase, 'action': action}
    if detail: entry['detail'] = detail
    if cost: entry['cost'] = cost
    if model: entry['model'] = model
    with open(LOG_FILE, 'a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')

def session_start():
    """Called from SessionStart hook.

    1. Zero-cost keyword correction scan (no LLM) on the most recent unharvested
       session — full LLM harvest runs on cron_harvest (every 30 minutes).
    2. Print compact belief index (Layer 1) for context injection
    3. Print pending suggestion reminders
    """
    init_evo_db()
    # S3：SessionStart 钩子只有 5s 超时，跑 LLM 收割（extract_observations）
    # 会在超时被杀——LLM 请求已发出但结果丢弃（白烧 tokens），且会话未标记
    # 收割导致 30 分钟后 cron 再收割一次（重复 LLM 花费 + 重复写入）。
    # 这里只做零成本的关键词纠正扫描（_detect_corrections，纯正则），
    # 不跑 LLM、不落盘、不标记，完整收割统一交给 cron/后台。
    if not _is_paused():
        try:
            from .harvester import get_unharvested_sessions, _detect_corrections
            conn = init_db()
            try:
                session_ids = get_unharvested_sessions(conn, limit=1)
                if session_ids:
                    sid = session_ids[0]
                    rows = conn.execute(
                        "SELECT role, content, timestamp FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
                        (sid,),
                    ).fetchall()
                    user_msgs = [{'content': r['content'], 'timestamp': r['timestamp']} for r in rows if r['role'] == 'user' and r['content']]
                    assistant_msgs = [{'content': r['content'], 'timestamp': r['timestamp']} for r in rows if r['role'] == 'assistant' and r['content']]
                    correction_count = len(_detect_corrections(user_msgs, assistant_msgs))
                    _log('observe', 'session_scan', {'session_id': sid, 'corrections': correction_count, 'mode': 'keyword_scan'})
            finally:
                conn.close()
        except Exception as e:
            _log('observe', 'harvest_error', {'error': str(e), 'mode': 'keyword_scan'})
    # Layer 1: compact belief index (~50-100 tokens)
    conn = init_honcho_db()
    if not get_user(conn, 'default'):
        create_user(conn, user_id='default')
    beliefs = get_beliefs(conn, 'default')
    if beliefs:
        print('[Memory] 可用记忆索引:')
        for b in beliefs[:10]:
            stage = belief_stage(b)
            print(f"  [{b['category']}] {b['content'][:40]}... (conf:{b['confidence']:.0%}, stage:{stage})")
        print(f'  共{len(beliefs)}条信念。用 memory_timeline 或 memory_detail 按需加载。')
    conn.close()
    # Behavior rules (compact)
    agent_rules = get_agent_rules(kind='behavior')
    if agent_rules:
        print('\n[Evolution] 行为规则提醒:')
        for r in agent_rules[:5]:
            if r.get('active'):
                eff = r.get('effectiveness', 0)
                eff_str = f' (eff:{eff:.0%})' if r.get('times_applied', 0) > 0 else ''
                print(f"  {r['content'][:60]}{eff_str}")
    # Pending suggestions
    suggestions = load_suggestions()
    pending = [s for s in suggestions if s['status'] == 'pending']
    if pending:
        print(f'\n[Evolution] {len(pending)}条待审批建议。用 evolution_approve/reject 处理。')

def cron_harvest():
    """Called from cron every 30 minutes. Harvest recent unharvested sessions."""
    if _is_paused():
        _log('observe', 'paused_skip', {})
        return {'status': 'paused'}
    result = harvest_recent_sessions(max_sessions=1)
    _log('observe', 'cron_harvest', result)
    return result

def cron_adapt(dry_run: bool = True):
    """Called from cron daily at 03:00. Run belief-to-action adaptation.

    S1：自动 cron 默认 dry_run=True —— 只生成待审批预览，不直接写全局
    ~/CLAUDE.md 或记忆文件，避免绕过审批管线。用户显式触发（evolution_run
    的 act 阶段，经由 Web/MCP 管理员接口）时才真实写入。
    """
    if _is_paused():
        return {'status': 'paused'}
    result = execute_adaptation_cycle(dry_run=dry_run)
    _log('act', 'cron_adapt', result)
    return result

def cron_reflect():
    """Called from cron weekly on Sunday at 04:00. Self-reflection."""
    if _is_paused():
        _log('reflect', 'paused_skip', {})
        return {'status': 'paused'}
    result = reflect_on_model('default')
    _log('reflect', 'cron_reflect', result)
    from .adapter import save_suggestions
    for action in result.get('suggested_actions', []):
        if action['action'] == 'promote_observation':
            save_suggestions([{'id': f"ref-{int(time.time())}-{action.get('category', 'gen')}", 'belief_id': '', 'type': 'belief_promotion', 'content': action.get('content', ''), 'suggestion': f"Promote observation to belief: {action.get('content', '')[:80]}", 'status': 'pending', 'created_at': time.time()}])
    return result

def cron_maintain():
    """Called from cron daily at 04:00. Decay, compress, archive, forget, merge, temporal abstract."""
    if _is_paused():
        return {'status': 'paused'}
    results = {}
    conn = init_honcho_db()
    # 1. Decay old beliefs
    try:
        decayed = decay_beliefs(conn, 'default')
        results['decayed'] = len(decayed)
        _log('maintain', 'decay', {'count': len(decayed)})
    except Exception:
        logger.exception("maintain: decay failed")
        results['decayed'] = -1
    # 2. Compress/merge similar beliefs
    try:
        compressed = compress_beliefs('default')
        results['compressed'] = compressed
        _log('maintain', 'compress', compressed)
    except Exception:
        logger.exception("maintain: compress failed")
        results['compressed'] = -1
    # 3. Merge semantically similar beliefs
    try:
        from .honcho.models import merge_similar_beliefs
        merged = merge_similar_beliefs(conn, 'default')
        results['merged'] = len(merged)
        _log('maintain', 'merge', {'count': len(merged)})
    except Exception:
        logger.exception("maintain: merge failed")
        results['merged'] = -1
    # 4. Forget low-value beliefs
    try:
        from .honcho.models import forget_beliefs
        forgotten = forget_beliefs(conn, 'default')
        results['forgotten'] = len(forgotten)
        _log('maintain', 'forget', {'count': len(forgotten), 'details': forgotten[:5]})
    except Exception:
        logger.exception("maintain: forget failed")
        results['forgotten'] = -1
    # 5. Archive old contradictions
    try:
        archived = archive_old_contradictions(conn, 'default', days=30)
        results['archived'] = archived
        _log('maintain', 'archive', {'count': archived})
    except Exception:
        logger.exception("maintain: archive failed")
        results['archived'] = -1
    # 6. Temporal abstraction: summarize trends over time
    try:
        temporal_summary = generate_temporal_abstraction(conn, 'default')
        results['temporal'] = 'ok'
        _log('maintain', 'temporal_abstraction', temporal_summary)
    except Exception:
        logger.exception("maintain: temporal_abstraction failed")
        results['temporal'] = -1
    conn.close()
    # 7. Analyze behavior patterns from correction observations
    try:
        from .behavior_analyzer import analyze_behavior_patterns
        behavior_result = analyze_behavior_patterns(days=7)
        results['behavior_patterns'] = behavior_result.get('patterns', [])
        _log('maintain', 'behavior_analyze', {
            'corrections': behavior_result.get('corrections_analyzed', 0),
            'patterns': len(behavior_result.get('patterns', [])),
        })
    except Exception:
        logger.exception("maintain: behavior analysis failed")
        results['behavior_patterns'] = -1
    # 8. Cost circuit breaker
    try:
        circuit = _check_cost_circuit()
        results['cost_circuit'] = circuit
    except Exception:
        logger.exception("maintain: cost circuit failed")
        results['cost_circuit'] = 'error'
    return results


def generate_temporal_abstraction(conn, user_id: str) -> dict:
    """Generate temporal abstractions: summarize belief trends over time.

    Groups beliefs by week/month and extracts trends:
    - Rising topics (increasing confidence)
    - Declining topics (decreasing confidence)
    - Stable topics (consistent confidence)
    - New topics (recently created)
    """
    import time
    now = time.time()
    cutoff_7d = now - 7 * 86400
    cutoff_30d = now - 30 * 86400

    # Get all beliefs with timestamps
    beliefs = get_beliefs(conn, user_id)

    # Categorize by time and trend
    rising = []
    declining = []
    stable = []
    new_this_week = []

    for b in beliefs:
        created = b.get('created_at', 0)
        updated = b.get('updated_at', 0)
        conf = b.get('confidence', 0.5)
        reinforcements = b.get('reinforcement_count', 0)

        # New this week
        if created > cutoff_7d:
            new_this_week.append(b)

        # Trend analysis based on reinforcements and confidence
        if reinforcements >= 3 and conf >= 0.7:
            rising.append(b)
        elif reinforcements == 0 and conf < 0.4:
            declining.append(b)
        elif conf >= 0.5:
            stable.append(b)

    # Generate temporal summary as a new belief
    summary = {
        'week': {
            'rising_count': len(rising),
            'declining_count': len(declining),
            'stable_count': len(stable),
            'new_count': len(new_this_week),
            'rising_topics': [b['content'][:60] for b in rising[:3]],
            'declining_topics': [b['content'][:60] for b in declining[:3]],
        },
        'generated_at': now,
    }

    # Clean up old temporal_abstraction beliefs (keep only the latest)
    from .honcho.models import add_belief
    try:
        cursor = conn.execute("SELECT id FROM beliefs WHERE category='temporal_abstraction' ORDER BY updated_at DESC")
        old_ids = [row[0] for row in cursor.fetchall()[1:]]
        if old_ids:
            placeholders = ','.join('?' * len(old_ids))
            conn.execute(f"DELETE FROM beliefs WHERE id IN ({placeholders})", old_ids)
            conn.commit()
    except Exception:
        logger.debug("temporal_abstraction: old belief cleanup failed", exc_info=True)
    temporal_content = (
        f"Temporal summary (week): {len(rising)} rising, {len(declining)} declining, "
        f"{len(stable)} stable, {len(new_this_week)} new beliefs. "
        f"Rising: {', '.join(summary['week']['rising_topics'][:2]) or 'none'}. "
        f"Declining: {', '.join(summary['week']['declining_topics'][:2]) or 'none'}."
    )
    add_belief(conn, user_id, 'temporal_abstraction', temporal_content, confidence=0.6)

    return summary

def cron_explore():
    """Called from cron weekly. Discover knowledge gaps and explore them.

    Delegates to knowledge_explorer.run_knowledge_exploration(), which:
    - caps at 2 topics per run and dedups recently-explored topics (cost gate),
    - falls back to a curated topic list when action_log shows no failures,
    - failure-isolates each topic so one bad explore can't abort the pass,
    - writes a markdown doc and ingests it into the knowledge base.
    """
    if _is_paused():
        return {'status': 'paused'}
    try:
        from .knowledge_explorer import run_knowledge_exploration
        result = run_knowledge_exploration(max_topics=2, dedup_days=14)
        _log('knowledge', 'cron_explore', {
            'gaps_found': result.get('gaps_found', 0),
            'source': result.get('source'),
            'topics_picked': result.get('topics_picked', 0),
            'skipped_recent': result.get('skipped_recent', 0),
            'results': result.get('results', []),
        })
        return result
    except Exception:
        logger.exception("cron_explore failed")
        return {'status': 'error'}

def cron_architect():
    """Called from cron weekly. Build architecture model and detect bottlenecks."""
    if _is_paused():
        return {'status': 'paused'}
    from .architect import build_architecture_model, detect_bottlenecks, propose_restructure
    from .evo_models import save_architecture_snapshot
    model = build_architecture_model()
    findings = detect_bottlenecks(model)
    proposals = propose_restructure(findings)
    model_json = json.dumps(model, ensure_ascii=False)
    save_architecture_snapshot(model_json, findings_count=len(findings), proposals_count=len(proposals))
    if proposals:
        from .adapter import save_suggestions
        save_suggestions(proposals)
        _log('architect', 'proposals_saved', {'count': len(proposals)})
    _log('architect', 'cron_architect', {'findings': len(findings), 'proposals': len(proposals), 'components': len(model.get('components', []))})
    return {'components': len(model.get('components', [])), 'findings': len(findings), 'proposals': len(proposals)}

def _estimate_daily_cost() -> float:
    """Estimate daily cost of evolution engine itself from audit_log.

    基于网络探索发现：Agent Memory系统需要量化记忆质量。
    改进：区分引擎成本和用户日常使用成本，避免混淆。
    S4：改用 SQL COALESCE(SUM(cost),0) 直接求和，避免 get_audit(limit=100)
    在日调用超过 100 条时截断导致成本被低估、熔断永不触发。
    """
    try:
        from .evo_models import get_audit_cost_since
        cutoff = time.time() - 86400
        # 只计算进化引擎自身的成本，排除对话成本
        evo_phases = ('observe', 'act', 'maintain', 'architect', 'knowledge', 'introspect', 'control')
        return get_audit_cost_since(cutoff, evo_phases)
    except Exception:
        return 0.0

def evolution_status() -> dict:
    """Return current evolution state — user profile from Honcho + agent rules from evo.db."""
    conn = init_honcho_db()
    if not get_user(conn, 'default'):
        create_user(conn, user_id='default')
    profile_beliefs = get_profile(conn, 'default').get('beliefs', [])
    profile_count = len([b for b in profile_beliefs if b['category'] != 'behavior_pattern'])
    conn.close()
    agent_rules = get_agent_rules()
    behavior_rules = [r for r in agent_rules if r['kind'] == 'behavior']
    action_stats = get_action_stats()
    by_stage = {'draft': 0, 'established': 0, 'core': 0}
    for b in profile_beliefs:
        if b['category'] != 'behavior_pattern':
            by_stage[belief_stage(b)] += 1
    from .evo_models import get_proposals
    pending_count = len(get_proposals(status='pending'))
    circuit = _get_circuit_state()
    return {'paused': _is_paused(), 'profile_beliefs': profile_count, 'behavior_rules': len(behavior_rules), 'total_agent_rules': len(agent_rules), 'by_stage': by_stage, 'pending_suggestions': pending_count, 'action_stats': action_stats, 'estimated_daily_cost': _estimate_daily_cost(), 'cost_circuit': circuit}

def evolution_run(stage: str='all') -> dict:
    """Manually trigger one or all evolution stages."""
    results = {}
    if stage in ('all', 'observe'):
        results['observe'] = cron_harvest()
    if stage in ('all', 'act'):
        # S1：用户显式运行 act 阶段才真实写入 CLAUDE.md/记忆文件
        results['act'] = cron_adapt(dry_run=False)
    if stage in ('all', 'reflect'):
        results['reflect'] = cron_reflect()
    if stage in ('all', 'maintain'):
        results['maintain'] = cron_maintain()
    if stage in ('all', 'explore'):
        results['explore'] = cron_explore()
    if stage in ('all', 'architect'):
        results['architect'] = cron_architect()
    if stage in ('all', 'learn'):
        results['learn'] = immediate_learn(cooldown_hours=0)
    return results

def evolution_update_cost_config(warn: float = None, pause: float = None, stop: float = None, auto_resume_hours: int = None) -> dict:
    """Update cost circuit breaker thresholds."""
    config = _load_cost_config()
    if warn is not None:
        config['warn_threshold'] = warn
    if pause is not None:
        config['pause_threshold'] = pause
    if stop is not None:
        config['stop_threshold'] = stop
    if auto_resume_hours is not None:
        config['auto_resume_hours'] = auto_resume_hours
    _save_cost_config(config)
    _log('control', 'cost_config_update', config)
    return {'status': 'updated', 'config': config}


def evolution_revert(adaptation_id: str) -> dict:
    """Revert a specific adaptation by removing the marked section from CLAUDE.md."""
    from .adapter import CLAUDE_MD, MARKER_START, MARKER_END
    if not CLAUDE_MD.exists():
        return {'status': 'no_claude_md'}
    content = CLAUDE_MD.read_text()
    if MARKER_START not in content:
        return {'status': 'no_learned_prefs'}
    start_idx = content.index(MARKER_START)
    end_idx = content.index(MARKER_END) + len(MARKER_END)
    new_content = content[:start_idx].rstrip() + '\n' + content[end_idx:].lstrip('\n')
    CLAUDE_MD.write_text(new_content)
    _log('revert', 'claude_md_remove', {'adaptation_id': adaptation_id})
    return {'status': 'reverted', 'removed_section': 'learned_prefs'}

def evolution_pause() -> dict:
    """Pause all evolution cron jobs."""
    EVOLUTION_DIR.mkdir(parents=True, exist_ok=True)
    PAUSE_FLAG.write_text(str(time.time()))
    _log('control', 'pause', {})
    return {'status': 'paused'}

def evolution_resume() -> dict:
    """Resume all evolution cron jobs."""
    if PAUSE_FLAG.exists():
        PAUSE_FLAG.unlink()
    # Reset cost circuit if resuming manually
    circuit = _get_circuit_state()
    if circuit['state'] in ('paused', 'stopped'):
        _log('control', 'resume_override', {'previous_state': circuit['state'], 'daily_cost': circuit['daily_cost']})
    _log('control', 'resume', {})
    return {'status': 'resumed'}

def track_rule_adherence(session_history: list[dict]) -> dict:
    """After a session exchange, update rule effectiveness based on corrections.

    NOTE (2026-08-11): the previous implementation added +1 to times_applied
    for EVERY active rule on EVERY session — that is a session counter, not a
    rule-application counter, and it polluted every rule's effectiveness
    (CLAUDE.md showed "(applied: 123x)" where 123 was session count). A rule is
    only "applied" when its content is actually exercised, which we cannot
    observe reliably here. Until a real per-rule event tracker exists, do not
    mutate the metrics on session end at all.
    """
    return {'updated': 0, 'note': 'disabled: session-count-based rule tracking polluted metrics'}


def session_end(session_id: str='') -> dict:
    """Called from SessionEnd hook.

    1. Track rule adherence for this session (updates effectiveness)
    2. Quick-scan current session for correction patterns (no LLM, keyword only)
    3. If corrections detected, immediately harvest this session
    4. Log session end event
    """
    if _is_paused():
        return {'status': 'paused'}
    if not session_id:
        try:
            conn = init_db()
            row = conn.execute('SELECT session_id FROM messages ORDER BY timestamp DESC LIMIT 1').fetchone()
            session_id = row['session_id'] if row else ''
            conn.close()
        except Exception:
            logger.exception("session_end: failed to get session_id")

    if not session_id:
        _log('session_end', 'no_session_id', {})
        return {'status': 'no_session_id'}

    # Track rule effectiveness based on session messages
    try:
        conn = init_db()
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY timestamp",
            (session_id,)
        ).fetchall()
        session_history = [dict(r) for r in rows]
        conn.close()
        if session_history:
            track_result = track_rule_adherence(session_history)
            _log('session_end', 'rule_tracking', track_result)
    except Exception as e:
        _log('session_end', 'rule_tracking_error', {'error': str(e)})

    try:
        conn = init_db()
        # Reuse the strict correction detector from harvester (tightened regex +
        # conversational filters) instead of a wide SQL LIKE that matched
        # ordinary negations (不对/不行/重复 …) and over-triggered the whole
        # immediate-learn pipeline on every session.
        rows = conn.execute(
            "SELECT role, content, timestamp FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,),
        ).fetchall()
        user_msgs = [{'content': r['content'], 'timestamp': r['timestamp']} for r in rows if r['role'] == 'user' and r['content']]
        assistant_msgs = [{'content': r['content'], 'timestamp': r['timestamp']} for r in rows if r['role'] == 'assistant' and r['content']]
        from .harvester import _detect_corrections
        correction_count = len(_detect_corrections(user_msgs, assistant_msgs))
        result = {'session_id': session_id, 'corrections_detected': correction_count}
        if correction_count > 0:
            try:
                from .harvester import harvest_session
                harvest_result = harvest_session(conn, session_id)
                result['harvest'] = harvest_result
                _log('session_end', 'harvested', {'corrections': correction_count})
            except Exception as e:
                _log('session_end', 'harvest_error', {'error': str(e)})
            # Be-ACTIVE: immediately turn corrections into rules/skill proposals
            # instead of waiting for the next daily cron. Runs in a detached
            # process so the 5s SessionEnd hook timeout isn't blown by the LLM
            # call inside immediate_learn.
            try:
                import subprocess
                proj_root = Path(__file__).resolve().parent.parent
                subprocess.Popen(
                    ['python3', '-c', 'from metano.evolution import immediate_learn; immediate_learn()'],
                    cwd=str(proj_root),
                    env=os.environ.copy(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                result['immediate_learn'] = {'status': 'spawned'}
            except Exception as e:
                _log('learn', 'immediate_learn_spawn_error', {'error': str(e)})
        else:
            _log('session_end', 'clean_session', {'session_id': session_id})
    except Exception:
        logger.exception("session_end: correction count failed")
        correction_count = 0
        result = {'session_id': session_id, 'corrections_detected': correction_count}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return result


def cron_introspect():
    """Cron entry point: scan own source code for anti-patterns and feed as observations."""
    if _is_paused():
        return {'status': 'paused'}
    try:
        from .code_introspector import introspect_and_report
        result = introspect_and_report()
        _log('introspect', 'code_scan', result)
        return result
    except Exception:
        logger.exception("cron_introspect failed")
        return {'status': 'error'}


def cron_evaluate():
    """Cron entry point: evaluate effects of recently applied proposals."""
    if _is_paused():
        return {'status': 'paused'}
    try:
        from .evolution_eval import evaluate_all_recent
        results = evaluate_all_recent()
        _log('evaluate', 'cron_evaluate', {'evaluated': len(results)})
        return {'evaluated': len(results), 'results': results}
    except Exception:
        logger.exception("cron_evaluate failed")
        return {'status': 'error'}


def immediate_learn(user_id: str = 'default', cooldown_hours: float = 2.0) -> dict:
    """Be-ACTIVE immediate learning: right after a correction-heavy session,
    turn the corrections into behavior-rule and skill-improvement proposals
    instead of waiting for the next daily cron.

    Mirrors Hermes' 'most sessions produce at least one skill update' posture:
    corrections are a first-class signal, so they are acted on at the moment
    they happen. Bounded by a cooldown (default 2h) to cap LLM spend — each
    pass is one behavior-analysis call plus skill matching.
    """
    if _is_paused():
        return {'status': 'paused'}
    from .evo_models import get_meta, set_meta
    last = get_meta('last_immediate_learn_ts')
    if last:
        try:
            elapsed_h = (time.time() - float(last)) / 3600
            if elapsed_h < cooldown_hours:
                _log('learn', 'cooldown_skip', {'elapsed_h': round(elapsed_h, 2), 'cooldown_h': cooldown_hours})
                return {'status': 'cooldown', 'elapsed_h': round(elapsed_h, 2)}
        except (ValueError, TypeError):
            pass
    set_meta('last_immediate_learn_ts', str(time.time()))
    result = {'status': 'completed'}
    # 1. Behavior rules from recent corrections (LLM, deduped against existing).
    try:
        from .behavior_analyzer import analyze_behavior_patterns
        rules = analyze_behavior_patterns(user_id, days=3)
        result['rules'] = {'corrections': rules.get('corrections_analyzed', 0), 'added': len(rules.get('patterns', []))}
        _log('learn', 'immediate_rules', result['rules'])
    except Exception:
        logger.exception("immediate_learn: behavior analysis failed")
        result['rules'] = {'status': 'error'}
    # 2. Skill-improvement proposals from recent corrections (approval-gated).
    try:
        from .skill_improvement import propose_skill_improvements
        conn = get_honcho_db()
        try:
            cutoff = time.time() - 3 * 86400
            rows = conn.execute(
                "SELECT content FROM observations WHERE user_id=? AND category='correction' AND timestamp>=? ORDER BY timestamp DESC LIMIT 20",
                (user_id, cutoff)).fetchall()
        finally:
            conn.close()
        corrections = [{'content': r['content']} for r in rows]
        skill_props = propose_skill_improvements(corrections, source='immediate_learn')
        result['skill_proposals'] = {'created': skill_props['proposals_created']}
        _log('learn', 'immediate_skill_proposals', result['skill_proposals'])
    except Exception:
        logger.exception("immediate_learn: skill proposals failed")
        result['skill_proposals'] = {'status': 'error'}
    return result