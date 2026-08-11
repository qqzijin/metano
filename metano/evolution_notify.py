"""Notify user via Feishu about pending evolution proposals requiring approval."""

import json
import os
import subprocess
from pathlib import Path
from metano.log import logger

# The recipient chat id comes from the environment (FEISHU_NOTIFY_CHAT_ID),
# never hardcoded — this repo is public and must not contain private ids.
NOTIFICATION_CHAT_ID = os.environ.get("FEISHU_NOTIFY_CHAT_ID", "")


def _send_feishu(text: str) -> tuple[int, str]:
    """Send via lark-cli; returns (returncode, stderr)."""
    result = subprocess.run(
        ['lark-cli', 'im', '+messages-send',
         '--chat-id', NOTIFICATION_CHAT_ID,
         '--as', 'bot',
         '--markdown', text],
        capture_output=True, text=True, timeout=15,
    )
    return result.returncode, result.stderr


def notify_pending_proposals(proposals: list[dict]) -> dict:
    """Send a Feishu message listing pending proposals for approval.

    Filters out test data, duplicates, and low-quality proposals before sending.
    Uses lark-cli to send as bot identity.
    """
    if not proposals:
        return {'status': 'no_proposals'}

    # Filter: skip test data and too-short content
    filtered = []
    seen_content = {}
    for p in proposals:
        # Skip test/e2e source
        if p.get('source') in ('test', 'e2e_test'):
            continue
        # Skip content shorter than 10 chars (likely garbage)
        if len(p.get('content', '')) < 10:
            continue
        # Deduplicate: same content prefix only keep first
        key = p['content'][:50]
        if key in seen_content:
            continue
        seen_content[key] = p['id']
        filtered.append(p)

    if not filtered:
        return {'status': 'no_valid_proposals'}

    lines = ["**进化系统有待审批的提案：**\n"]
    for p in filtered:
        pid = p['id']
        ptype = p['proposal_type']
        content = p['content'][:80]
        lines.append(f"**#{pid}** [{ptype}] {content}")
    lines.append("\n回复 `批准#ID` 或 `拒绝#ID` 来操作")

    text = '\n'.join(lines)

    try:
        if not NOTIFICATION_CHAT_ID:
            logger.warning("FEISHU_NOTIFY_CHAT_ID not set; skip Feishu notification")
            return {'status': 'not_configured'}
        rc, err = _send_feishu(text)
        if rc == 0:
            logger.info(f"Notified {len(proposals)} pending proposals via Feishu")
            return {'status': 'sent', 'count': len(proposals)}
        else:
            logger.warning(f"Feishu notify failed: {err}")
            return {'status': 'failed', 'error': err[:200]}
    except Exception:
        logger.exception()
        return {'status': 'error'}


def process_approval_reply(text: str, sender_id: str = None, allowed_senders: list = None) -> dict | None:
    """Parse a Feishu reply like '批准#3' or '拒绝#5' and update proposal status.

    Returns the action taken, or None if text doesn't match.

    S2：当调用方提供了审批人白名单 allowed_senders 时，必须先校验 sender_id。
    非白名单发送者返回 {'action': 'denied', ...}，绝不改变 proposal 状态。
    未提供白名单时保持旧行为（向后兼容，如单元测试直接调用）。
    """
    import re
    # S2：审批人身份校验（仅当调用方提供白名单时强制）
    if allowed_senders:
        if not sender_id or sender_id not in allowed_senders:
            logger.warning(f"Feishu 审批回复被拒绝：发送者 {sender_id} 不在审批人白名单")
            return {'action': 'denied', 'reason': 'unauthorized_sender'}
    approve_match = re.match(r'批准\s*#?(\d+)', text.strip())
    reject_match = re.match(r'拒绝\s*#?(\d+)', text.strip())

    if approve_match:
        proposal_id = int(approve_match.group(1))
        from .evo_models import update_proposal_status
        update_proposal_status(proposal_id, 'approved')
        logger.info(f"Proposal #{proposal_id} approved via Feishu reply")
        return {'action': 'approved', 'proposal_id': proposal_id}

    if reject_match:
        proposal_id = int(reject_match.group(1))
        from .evo_models import update_proposal_status
        update_proposal_status(proposal_id, 'rejected')
        logger.info(f"Proposal #{proposal_id} rejected via Feishu reply")
        return {'action': 'rejected', 'proposal_id': proposal_id}

    return None
