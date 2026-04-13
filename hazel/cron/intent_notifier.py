"""Pure-code intent notification checker.

Replaces the LLM-based intent notification cron job with deterministic
Python logic.  Every 5 minutes, this module:

1. Queries the intents SQLite DB for due/overdue items
2. Filters out snoozed (still future), future-due, and recently-fired intents
3. Un-snoozes intents whose snooze_until has expired
4. Formats a concise notification message
5. Publishes an OutboundMessage to the bus
6. Updates last_fired_at on each notified intent

No LLM calls are made at any point.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Intents notified within this window are suppressed to avoid spam.
RECENTLY_FIRED_WINDOW = timedelta(hours=2)

# Query window: how far back to look for overdue intents.
LOOKBACK_WINDOW = timedelta(hours=1)

# Type labels used in the notification message.
_TYPE_EMOJI = {
    "task": "[Task]",
    "reminder": "[Reminder]",
    "event": "[Event]",
    "followup": "[Follow-up]",
}

_PRIORITY_LABELS = {
    0: "",
    1: "",
    2: " (high priority)",
    3: " (urgent)",
}


# ---------------------------------------------------------------------------
# DB helpers (mirrors intents.py without importing Tool classes)
# ---------------------------------------------------------------------------

def _get_db(workspace: Path) -> sqlite3.Connection:
    """Open (or reuse) a WAL-mode connection to the intents database."""
    from hazel.agent.tools.intents import _get_db as _intents_get_db

    return _intents_get_db(workspace)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_iso(dt: datetime) -> str:
    """Format a datetime as ISO8601 UTC with Z suffix."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_display(iso: str | None) -> str:
    """Format an ISO timestamp for human display."""
    if not iso:
        return "no date"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M") + " UTC"
    except (ValueError, TypeError):
        return iso


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _query_candidate_intents(
    conn: sqlite3.Connection,
    now: datetime,
) -> list[dict[str, Any]]:
    """Return intents that are potentially due or overdue.

    This mirrors the SQL from IntentListDueTool with include_overdue=True,
    using a 1-hour lookback window.
    """
    window_start = _fmt_iso(now - LOOKBACK_WINDOW)
    window_end = _fmt_iso(now)

    sql = (
        "SELECT * FROM intents "
        "WHERE status NOT IN ('done', 'canceled') "
        "  AND (snooze_until IS NULL OR snooze_until <= ?) "
        "  AND ("
        "    (due_at IS NOT NULL AND due_at <= ?)"
        "    OR (start_at IS NOT NULL AND start_at < ? AND (end_at IS NULL OR end_at > ?))"
        "  ) "
        "ORDER BY COALESCE(due_at, start_at) ASC, priority DESC, created_at ASC "
        "LIMIT 200"
    )
    rows = conn.execute(sql, [window_start, window_end, window_end, window_start]).fetchall()
    return [dict(r) for r in rows]


def _filter_and_prepare(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """Apply the notification rules and return intents that qualify.

    Side-effect: un-snoozes intents whose snooze_until has expired.
    """
    now_iso = _fmt_iso(now)
    fired_cutoff = _fmt_iso(now - RECENTLY_FIRED_WINDOW)
    qualifying: list[dict[str, Any]] = []

    for intent in candidates:
        status = intent["status"]
        snooze_until = intent.get("snooze_until")
        due_at = intent.get("due_at")
        last_fired = intent.get("last_fired_at")

        # Rule 1: snoozed with future snooze_until → skip
        if status == "snoozed" and snooze_until and snooze_until > now_iso:
            continue

        # Rule 1b: snoozed with no snooze_until → skip (anomalous state)
        if status == "snoozed" and not snooze_until:
            continue

        # Rule 2: snoozed with past snooze_until → un-snooze to active
        if status == "snoozed" and snooze_until and snooze_until <= now_iso:
            conn.execute(
                "UPDATE intents SET status = 'active', snooze_until = NULL, "
                "updated_at = ? WHERE id = ?",
                (now_iso, intent["id"]),
            )
            conn.commit()
            intent["status"] = "active"
            intent["snooze_until"] = None

        # Rule 3: active with future due_at → skip (not due yet)
        if intent["status"] == "active" and due_at and due_at > now_iso:
            continue

        # Rule 4: already notified recently → skip
        if last_fired and last_fired >= fired_cutoff:
            continue

        qualifying.append(intent)

    return qualifying


def _format_notification(intents: list[dict[str, Any]]) -> str:
    """Build a concise notification message from qualifying intents."""
    if len(intents) == 1:
        intent = intents[0]
        label = _TYPE_EMOJI.get(intent["type"], "[Intent]")
        prio = _PRIORITY_LABELS.get(intent.get("priority", 1), "")
        due = _fmt_display(intent.get("due_at") or intent.get("start_at"))
        body = f"\n{intent['body']}" if intent.get("body") else ""
        return f"{label} {intent['title']}{prio}\nDue: {due}{body}"

    lines = [f"You have {len(intents)} due items:\n"]
    for intent in intents:
        label = _TYPE_EMOJI.get(intent["type"], "[Intent]")
        prio = _PRIORITY_LABELS.get(intent.get("priority", 1), "")
        due = _fmt_display(intent.get("due_at") or intent.get("start_at"))
        lines.append(f"- {label} {intent['title']}{prio} (due: {due})")
    return "\n".join(lines)


def _mark_fired(
    conn: sqlite3.Connection,
    intents: list[dict[str, Any]],
    now: datetime,
) -> None:
    """Set last_fired_at on all notified intents."""
    now_iso = _fmt_iso(now)
    for intent in intents:
        conn.execute(
            "UPDATE intents SET last_fired_at = ?, updated_at = ? WHERE id = ?",
            (now_iso, now_iso, intent["id"]),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def check_and_notify(
    workspace: Path,
    channel: str,
    chat_id: str,
    bus: Any,
    *,
    _now: datetime | None = None,
) -> str | None:
    """Run the intent notification check. Returns the message sent, or None.

    Parameters
    ----------
    workspace : Path
        Agent workspace directory (contains ``data/intents.db``).
    channel : str
        Target channel name (e.g. ``"telegram"``).
    chat_id : str
        Target chat/user ID on that channel.
    bus : MessageBus
        The async message bus for publishing outbound messages.
    _now : datetime, optional
        Override for "now" (for testing). Must be timezone-aware UTC.
    """
    from hazel.bus.events import OutboundMessage

    now = _now or _now_utc()

    db_path = workspace / "data" / "intents.db"
    if not db_path.exists():
        logger.debug("intent_notifier: no intents.db at {}, skipping", db_path)
        return None

    conn = _get_db(workspace)
    candidates = _query_candidate_intents(conn, now)

    if not candidates:
        logger.debug("intent_notifier: no candidate intents found")
        return None

    qualifying = _filter_and_prepare(conn, candidates, now)

    if not qualifying:
        logger.debug("intent_notifier: no qualifying intents after filtering")
        return None

    message = _format_notification(qualifying)
    _mark_fired(conn, qualifying, now)

    await bus.publish_outbound(OutboundMessage(
        channel=channel,
        chat_id=chat_id,
        content=message,
    ))

    logger.info(
        "intent_notifier: sent {} notification(s) to {}:{}",
        len(qualifying),
        channel,
        chat_id,
    )
    return message
