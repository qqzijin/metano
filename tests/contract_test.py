#!/usr/bin/env python3
"""Contract test: verify backend API responses match frontend TypeScript types.

Usage: python3 contract_test.py [--base-url http://localhost:9120]

Exit code 0 = all pass, 1 = mismatches found.
"""

import json
import sys
import urllib.request
import urllib.error
from typing import Any

BASE = "http://localhost:9120"


def fetch(path: str) -> dict | list:
    url = f"{BASE}{path}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"__error__": str(e)}


def first_item(data: Any, wrapper_keys: list[str] = ("items", "providers", "tools", "results")) -> dict | None:
    """Extract first item from a wrapped response."""
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        for k in wrapper_keys:
            if k in data and isinstance(data[k], list) and data[k]:
                return data[k][0]
    return None


def check_fields(label: str, item: dict, expected: dict) -> list[str]:
    """Check that expected fields exist in item. Returns list of errors."""
    errors = []
    for field, expected_type in expected.items():
        if field not in item:
            errors.append(f"  MISSING: .{field} not in response")
        elif expected_type and not isinstance(item[field], expected_type):
            if expected_type is float and isinstance(item[field], int):
                continue  # int is fine for float
            errors.append(f"  TYPE: .{field} expected {expected_type.__name__}, got {type(item[field]).__name__} = {item[field]!r}")
    return errors


def check_ts_field(label: str, item: dict, field: str) -> list[str]:
    """Check a timestamp field can be either Unix REAL or ISO string."""
    errors = []
    if field not in item:
        return []
    val = item[field]
    if val is None:
        return []
    if isinstance(val, (int, float)):
        if val < 1e9 or val > 2e10:
            errors.append(f"  TS_RANGE: .{field} = {val} looks like not Unix epoch")
    elif isinstance(val, str):
        if not val[0:4].isdigit():
            errors.append(f"  TS_FORMAT: .{field} = {val!r} not ISO-like")
    else:
        errors.append(f"  TS_TYPE: .{field} unexpected type {type(val).__name__}")
    return errors


def run_tests() -> list[str]:
    errors: list[str] = []

    # === 1. /api/status ===
    print("1. /api/status")
    d = fetch("/api/status")
    if "__error__" in d:
        errors.append(f"1. /api/status: {d['__error__']}")
    else:
        errs = check_fields("status", d, {
            "status": str, "sessions": int, "messages": int, "skills_count": int,
        })
        # evolution sub-object
        if "evolution" in d and isinstance(d["evolution"], dict):
            errs += check_fields("status.evolution", d["evolution"], {
                "paused": bool, "total_beliefs": int, "pending_suggestions": int,
            })
        else:
            errs.append("  MISSING: .evolution sub-object")
        errors.extend(f"1. {e}" for e in errs)

    # === 2. /api/sessions ===
    print("2. /api/sessions")
    d = fetch("/api/sessions?limit=2")
    item = first_item(d, ["items"])
    if not item:
        errors.append("2. /api/sessions: no items returned")
    else:
        errs = check_fields("session", item, {
            "id": str, "message_count": int, "input_tokens": int, "output_tokens": int,
            "estimated_cost_usd": (int, float),
        })
        # Timestamps should be Unix REAL
        for f in ("started_at", "last_active"):
            errs += check_ts_field("session", item, f)
        # Check tool_call_count exists (backend returns it, frontend should handle)
        if "tool_call_count" not in item:
            errs.append("  INFO: .tool_call_count not returned (frontend doesn't use it yet)")
        errors.extend(f"2. {e}" for e in errs)

    # === 3. /api/sessions/:id/messages ===
    print("3. /api/sessions/:id/messages")
    sid = item["id"] if item else None
    if sid:
        d = fetch(f"/api/sessions/{sid}/messages?limit=3")
        msg_item = first_item(d, ["items"])
        if not msg_item:
            errors.append("3. messages: no items returned")
        else:
            # Backend returns id as number, frontend types say string
            if isinstance(msg_item.get("id"), int):
                errors.append("3.  MISMATCH: .id is number in backend, but string in TS Message interface")
            errs = check_fields("message", msg_item, {
                "role": str, "content": str,
            })
            errs += check_ts_field("message", msg_item, "timestamp")
            errors.extend(f"3. {e}" for e in errs)

    # === 4. /api/evolution ===
    print("4. /api/evolution")
    d = fetch("/api/evolution")
    if "__error__" in d:
        errors.append(f"4. /api/evolution: {d['__error__']}")
    else:
        errs = check_fields("evolution", d, {
            "paused": bool, "total_beliefs": int, "pending_suggestions": int,
        })
        if "by_stage" not in d:
            errs.append("  MISSING: .by_stage")
        errors.extend(f"4. {e}" for e in errs)

    # === 5. /api/evolution/suggestions ===
    print("5. /api/evolution/suggestions")
    d = fetch("/api/evolution/suggestions")
    item = first_item(d, ["items"])
    if not item:
        print("  (no suggestions - skipped)")
    else:
        errs = check_fields("suggestion", item, {
            "id": str, "type": str, "content": str, "status": str,
        })
        # belief_id should exist
        if "belief_id" not in item:
            errs.append("  MISSING: .belief_id")
        # suggestion field should exist
        if "suggestion" not in item:
            errs.append("  MISSING: .suggestion")
        errors.extend(f"5. {e}" for e in errs)

    # === 6. /api/models ===
    print("6. /api/models")
    d = fetch("/api/models")
    item = first_item(d, ["items"])
    if not item:
        errors.append("6. /api/models: no items returned")
    else:
        errs = check_fields("model_provider", item, {
            "name": str, "model": str, "is_default": bool,
        })
        errors.extend(f"6. {e}" for e in errs)

    # === 7. /api/proxy/providers ===
    print("7. /api/proxy/providers")
    d = fetch("/api/proxy/providers")
    item = first_item(d, ["providers"])
    if not item:
        errors.append("7. /api/proxy/providers: no items returned")
    else:
        errs = check_fields("proxy_provider", item, {
            "name": str, "base_url": str, "model": str,
        })
        if "note" not in item:
            errs.append("  INFO: .note field missing from ModelProvider TS interface")
        errors.extend(f"7. {e}" for e in errs)

    # === 8. /api/knowledge ===
    print("8. /api/knowledge")
    d = fetch("/api/knowledge")
    item = first_item(d, ["items"])
    if not item:
        errors.append("8. /api/knowledge: no items returned")
    else:
        errs = check_fields("knowledge_doc", item, {
            "doc_id": str, "chunk_count": int,
        })
        # Timestamp check
        errs += check_ts_field("knowledge_doc", item, "updated_at")
        errors.extend(f"8. {e}" for e in errs)

    # === 9. /api/profiles/default ===
    print("9. /api/profiles/default")
    d = fetch("/api/profiles/default")
    if "__error__" in d:
        errors.append(f"9. /api/profiles/default: {d['__error__']}")
    else:
        beliefs = d.get("beliefs", [])
        if not beliefs:
            print("  (no beliefs - skipped)")
        else:
            b = beliefs[0]
            # Backend has no .stage field; frontend must compute it
            if "stage" not in b:
                print("  INFO: backend beliefs lack .stage field (frontend must compute from confidence+reinforcement_count)")
            errs = check_fields("belief", b, {
                "id": str, "category": str, "content": str, "confidence": (int, float),
            })
            errors.extend(f"9. {e}" for e in errs)

    # === 10. /api/memory/stats ===
    print("10. /api/memory/stats")
    d = fetch("/api/memory/stats")
    if "__error__" in d:
        errors.append(f"10. /api/memory/stats: {d['__error__']}")
    else:
        errs = check_fields("memory_stats", d, {
            "total_memories": int, "avg_importance": (int, float),
        })
        if "by_category" not in d:
            errs.append("  MISSING: .by_category")
        errors.extend(f"10. {e}" for e in errs)

    # === 11. /api/memory/export ===
    print("11. /api/memory/export")
    d = fetch("/api/memory/export")
    if "__error__" in d:
        errors.append(f"11. /api/memory/export: {d['__error__']}")
    else:
        mems = d.get("memories", [])
        if not mems:
            print("  (no memories - skipped)")
        else:
            m = mems[0]
            errs = check_fields("memory_entry", m, {
                "id": int, "content": str, "category": str, "importance": (int, float),
            })
            errors.extend(f"11. {e}" for e in errs)

    # === 12. /api/mcp/tools ===
    print("12. /api/mcp/tools")
    d = fetch("/api/mcp/tools")
    item = first_item(d, ["tools"])
    if not item:
        errors.append("12. /api/mcp/tools: no items returned")
    else:
        errs = check_fields("mcp_tool", item, {
            "name": str, "source": str, "description": str,
        })
        errors.extend(f"12. {e}" for e in errs)

    # === 13. /api/cron/jobs ===
    print("13. /api/cron/jobs")
    d = fetch("/api/cron/jobs")
    jobs = d if isinstance(d, list) else d.get("items", d.get("jobs", []))
    if not jobs:
        print("  (no cron jobs - skipped)")
    else:
        j = jobs[0]
        errs = check_fields("cron_job", j, {
            "id": str, "name": str, "prompt": str, "enabled": bool,
        })
        if "schedule" not in j:
            errs.append("  MISSING: .schedule")
        elif isinstance(j["schedule"], dict):
            if "kind" not in j["schedule"] or "expr" not in j["schedule"]:
                errs.append("  SHAPE: .schedule should have .kind and .expr")
        errors.extend(f"13. {e}" for e in errs)

    # === 14. /api/analytics ===
    print("14. /api/analytics")
    d = fetch("/api/analytics?days=7")
    if "__error__" in d:
        errors.append(f"14. /api/analytics: {d['__error__']}")
    else:
        total = d.get("total", {})
        errs = check_fields("analytics.total", total, {
            "session_count": int, "input_tokens": int, "output_tokens": int,
            "estimated_cost_usd": (int, float),
        })
        daily = d.get("daily", [])
        if daily:
            day_item = daily[0]
            errs += check_fields("analytics.daily", day_item, {
                "day": str, "input_tokens": int, "output_tokens": int,
            })
        by_model = d.get("by_model", [])
        if by_model:
            model_item = by_model[0]
            errs += check_fields("analytics.by_model", model_item, {
                "model": (str, type(None)), "session_count": int, "input_tokens": int,
                "output_tokens": int, "estimated_cost_usd": (int, float),
            })
        errors.extend(f"14. {e}" for e in errs)

    # === 15. /api/search ===
    print("15. /api/search?q=test")
    d = fetch("/api/search?q=test")
    if "__error__" in d:
        errors.append(f"15. /api/search: {d['__error__']}")
    else:
        results = d.get("results", [])
        if not results:
            print("  (no results - skipped)")
        else:
            r = results[0]
            errs = check_fields("search_result", r, {
                "session_id": str, "role": str, "snippet": str,
            })
            errs += check_ts_field("search_result", r, "timestamp")
            errors.extend(f"15. {e}" for e in errs)

    # === 16. /api/evolution/behaviors ===
    print("16. /api/evolution/behaviors")
    d = fetch("/api/evolution/behaviors")
    if "__error__" in d:
        errors.append(f"16. /api/evolution/behaviors: {d['__error__']}")
    else:
        if "patterns" not in d:
            errors.append("16. MISSING: .patterns array")
        if "recent_corrections" not in d:
            errors.append("16. MISSING: .recent_corrections array")
        for p in d.get("patterns", [])[:1]:
            errs = check_fields("behavior_pattern", p, {
                "id": str, "category": str, "content": str, "confidence": (int, float),
            })
            errors.extend(f"16. {e}" for e in errs)

    return errors


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--base-url":
        BASE = sys.argv[2]

    print(f"Contract test against {BASE}\n")
    errors = run_tests()
    print(f"\n{'='*50}")

    if errors:
        print(f"FAIL: {len(errors)} issue(s) found\n")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        print("PASS: All contracts verified\n")
        sys.exit(0)