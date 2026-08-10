"""JSONL → SQLite incremental/full indexer for Claude Code sessions."""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from .db import DB_PATH, init_db
from metano.log import logger
SESSIONS_DIR = Path.home() / '.claude' / 'projects'
MODEL_PRICING = {'claude-sonnet-4-6': {'input': 3.0, 'output': 15.0, 'cache_read': 0.3}, 'claude-opus-4-7': {'input': 15.0, 'output': 75.0, 'cache_read': 1.5}, 'claude-haiku-4-5-20251001': {'input': 0.8, 'output': 4.0, 'cache_read': 0.08}}

def parse_timestamp(ts) -> float:
    """Parse ISO 8601 string or numeric timestamp to epoch float."""
    if ts is None:
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            return dt.timestamp()
        except (ValueError, AttributeError):
            return 0.0
    return 0.0

def estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int=0) -> float:
    """Estimate USD cost for a model's token usage.

    Prefers the configurable price table (model_router.estimate_cost), which
    covers deepseek-v4-flash (0.14/0.28) etc.; falls back to MODEL_PRICING.
    """
    try:
        from .model_router import model_router
        return model_router.estimate_cost(model, input_tokens, output_tokens, cache_read)
    except Exception:
        pass
    pricing = MODEL_PRICING.get(model, MODEL_PRICING['claude-sonnet-4-6'])
    return input_tokens / 1000000 * pricing['input'] + output_tokens / 1000000 * pricing['output'] + cache_read / 1000000 * pricing['cache_read']

def extract_text(content) -> str:
    """Extract readable text from Claude message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get('type') == 'text':
                    parts.append(block.get('text', ''))
                elif block.get('type') == 'tool_use':
                    name = block.get('name', '')
                    inp = json.dumps(block.get('input', {}), ensure_ascii=False)[:500]
                    parts.append(f'[tool:{name}] {inp}')
                elif block.get('type') == 'tool_result':
                    c = block.get('content', '')
                    if isinstance(c, str):
                        parts.append(c[:500])
                    elif isinstance(c, list):
                        for sub in c:
                            if isinstance(sub, dict) and sub.get('type') == 'text':
                                parts.append(sub.get('text', '')[:500])
        return '\n'.join(parts)
    return ''

def extract_tool_info(content) -> tuple[Optional[str], str]:
    """Extract primary tool name and JSON array of all tool calls from content."""
    if not isinstance(content, list):
        return (None, '[]')
    tool_name = None
    tool_calls = []
    for block in content:
        if isinstance(block, dict) and block.get('type') == 'tool_use':
            if tool_name is None:
                tool_name = block.get('name')
            tool_calls.append({'name': block.get('name'), 'input_keys': list(block.get('input', {}).keys())})
    return (tool_name, json.dumps(tool_calls, ensure_ascii=False))

def parse_jsonl_file(filepath: Path, start_offset: int=0) -> list[dict]:
    """Parse a Claude Code JSONL file from a byte offset, returning raw records."""
    records = []
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        f.seek(start_offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records

def process_records(conn: sqlite3.Connection, session_id: str, project: str, records: list[dict]):
    """Process JSONL records and upsert into the database."""
    session_data = {'id': session_id, 'project': project, 'title': None, 'model': None, 'started_at': time.time(), 'ended_at': None, 'last_active': 0, 'message_count': 0, 'tool_call_count': 0, 'input_tokens': 0, 'output_tokens': 0, 'cache_read_tokens': 0, 'estimated_cost_usd': 0.0}
    messages = []
    for rec in records:
        rtype = rec.get('type', '')
        msg = rec.get('message', {})
        ts = parse_timestamp(rec.get('timestamp') or rec.get('t'))
        if ts and (session_data['started_at'] == 0 or ts < session_data['started_at']):
            session_data['started_at'] = ts
        if ts and ts > session_data['last_active']:
            session_data['last_active'] = ts
        if rtype == 'assistant':
            model = msg.get('model', '')
            if model:
                session_data['model'] = model
            usage = msg.get('usage', {})
            inp = usage.get('input_tokens', 0) or 0
            out = usage.get('output_tokens', 0) or 0
            cache_r = usage.get('cache_read_input_tokens', 0) or 0
            session_data['input_tokens'] += inp
            session_data['output_tokens'] += out
            # cache_read_input_tokens is a cumulative per-session counter in the
            # claude transcript (grows to the session total), NOT a per-turn value.
            # Summing it overcounts by ~100x; take the running max as the total.
            if cache_r > session_data['cache_read_tokens']:
                session_data['cache_read_tokens'] = cache_r
            content = msg.get('content', '')
            text = extract_text(content)
            tool_name, tool_calls = extract_tool_info(content)
            if tool_name:
                session_data['tool_call_count'] += 1
            messages.append({'session_id': session_id, 'role': 'assistant', 'content': text, 'tool_name': tool_name, 'tool_calls': tool_calls, 'timestamp': ts or time.time(), 'input_tokens': inp, 'output_tokens': out, 'duration_ms': None})
        elif rtype == 'user':
            content = msg.get('content', '')
            text = extract_text(content)
            messages.append({'session_id': session_id, 'role': 'user', 'content': text, 'tool_name': None, 'tool_calls': '[]', 'timestamp': ts or time.time(), 'input_tokens': 0, 'output_tokens': 0, 'duration_ms': None})
        elif rtype == 'ai-title':
            session_data['title'] = rec.get('aiTitle', '')
        elif rtype == 'system' and rec.get('subtype') == 'turn_duration':
            dur = rec.get('durationMs', 0)
            if messages:
                messages[-1]['duration_ms'] = dur
    session_data['message_count'] = len(messages)
    if session_data['last_active'] > 0:
        session_data['ended_at'] = session_data['last_active']
    session_data['estimated_cost_usd'] = estimate_cost(
        session_data['model'] or '', session_data['input_tokens'],
        session_data['output_tokens'], session_data['cache_read_tokens'])
    conn.execute('\n        INSERT INTO sessions (id, project, title, model, started_at, ended_at, last_active,\n                              message_count, tool_call_count, input_tokens, output_tokens,\n                              cache_read_tokens, estimated_cost_usd)\n        VALUES (:id, :project, :title, :model, :started_at, :ended_at, :last_active,\n                :message_count, :tool_call_count, :input_tokens, :output_tokens,\n                :cache_read_tokens, :estimated_cost_usd)\n        ON CONFLICT(id) DO UPDATE SET\n            title=COALESCE(excluded.title, sessions.title),\n            model=excluded.model,\n            ended_at=excluded.ended_at,\n            last_active=excluded.last_active,\n            message_count=excluded.message_count,\n            tool_call_count=excluded.tool_call_count,\n            input_tokens=excluded.input_tokens,\n            output_tokens=excluded.output_tokens,\n            cache_read_tokens=excluded.cache_read_tokens,\n            estimated_cost_usd=excluded.estimated_cost_usd\n    ', session_data)
    conn.execute('DELETE FROM messages WHERE session_id = ?', (session_id,))
    for m in messages:
        conn.execute('\n            INSERT INTO messages (session_id, role, content, tool_name, tool_calls,\n                                  timestamp, input_tokens, output_tokens, duration_ms)\n            VALUES (:session_id, :role, :content, :tool_name, :tool_calls,\n                    :timestamp, :input_tokens, :output_tokens, :duration_ms)\n        ', m)

def index_file(conn: sqlite3.Connection, filepath: Path, project: str, force: bool=False):
    """Index a single JSONL file, incrementally if possible."""
    session_id = filepath.stem
    stat = filepath.stat()
    file_size = stat.st_size
    file_mtime = stat.st_mtime
    if not force:
        row = conn.execute('SELECT last_byte_offset, last_modified FROM _index_state WHERE file_path = ?', (str(filepath),)).fetchone()
        if row and row['last_byte_offset'] >= file_size and (row['last_modified'] >= file_mtime):
            return
    start_offset = 0
    if not force and row and (row['last_modified'] >= file_mtime):
        start_offset = row['last_byte_offset']
    records = parse_jsonl_file(filepath, start_offset)
    if records:
        process_records(conn, session_id, project, records)
    conn.execute('INSERT OR REPLACE INTO _index_state (file_path, last_byte_offset, last_modified) VALUES (?, ?, ?)', (str(filepath), file_size, file_mtime))
    conn.commit()

def index_all(conn: Optional[sqlite3.Connection]=None, force: bool=False):
    """Index all Claude Code session JSONL files across all projects."""
    if conn is None:
        conn = init_db()
    count = 0
    for project_dir in SESSIONS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        project = project_dir.name
        for jsonl_file in project_dir.glob('*.jsonl'):
            try:
                index_file(conn, jsonl_file, project, force=force)
                count += 1
            except Exception as e:
                logger.exception()
                print(f'Error indexing {jsonl_file}: {e}')
    print(f'Indexed {count} session files')
    return count
if __name__ == '__main__':
    import sys
    force = '--full' in sys.argv or '--force' in sys.argv
    conn = init_db()
    index_all(conn, force=force)