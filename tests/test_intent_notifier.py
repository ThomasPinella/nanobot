"""Tests for the pure-code intent notification system.

Covers every branch in hazel/cron/intent_notifier.py:
- No intents DB → no notification
- No due intents → no notification
- Due intents are notified and last_fired_at is updated
- Snoozed intents with future snooze_until are skipped
- Snoozed intents with expired snooze_until are un-snoozed and notified
- Active intents with future due_at are skipped
- Recently-fired intents (last_fired_at within 2h) are skipped
- Multiple intents produce a single aggregated message
- Single intent produces a focused message
- Priority labels appear in the message
- Message formatting for all intent types
- Integration with the cron system_event dispatch
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from hazel.cron.intent_notifier import (
    LOOKBACK_WINDOW,
    RECENTLY_FIRED_WINDOW,
    _filter_and_prepare,
    _fmt_display,
    _fmt_iso,
    _format_notification,
    _mark_fired,
    _query_candidate_intents,
    check_and_notify,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _setup_db(workspace: Path) -> sqlite3.Connection:
    """Create a fresh intents.db with schema, return connection."""
    from hazel.agent.tools.intents import _SCHEMA_SQL

    db_dir = workspace / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "intents.db"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    return conn


def _insert_intent(
    conn: sqlite3.Connection,
    *,
    id: str = "TEST01",
    type: str = "reminder",
    title: str = "Test intent",
    body: str | None = None,
    status: str = "active",
    priority: int = 1,
    due_at: str | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
    snooze_until: str | None = None,
    last_fired_at: str | None = None,
    rrule: str | None = None,
) -> None:
    now_iso = _iso(_now())
    conn.execute(
        "INSERT INTO intents "
        "(id, type, title, body, status, priority, due_at, start_at, end_at, "
        "rrule, snooze_until, last_fired_at, created_at, updated_at, deferrals, rescheduled_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
        (id, type, title, body, status, priority, due_at, start_at, end_at,
         rrule, snooze_until, last_fired_at, now_iso, now_iso),
    )
    conn.commit()


class FakeBus:
    """Minimal bus mock that records published outbound messages."""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def publish_outbound(self, msg: Any) -> None:
        self.messages.append(msg)


# ---------------------------------------------------------------------------
# _fmt_display
# ---------------------------------------------------------------------------

class TestFmtDisplay:
    def test_none_returns_no_date(self):
        assert _fmt_display(None) == "no date"

    def test_valid_iso(self):
        assert _fmt_display("2026-04-13T14:30:00Z") == "2026-04-13 14:30 UTC"

    def test_invalid_string_returned_as_is(self):
        assert _fmt_display("not-a-date") == "not-a-date"


# ---------------------------------------------------------------------------
# _fmt_iso
# ---------------------------------------------------------------------------

class TestFmtIso:
    def test_formats_utc_datetime(self):
        dt = datetime(2026, 4, 13, 10, 0, 0, tzinfo=timezone.utc)
        assert _fmt_iso(dt) == "2026-04-13T10:00:00Z"


# ---------------------------------------------------------------------------
# _query_candidate_intents
# ---------------------------------------------------------------------------

class TestQueryCandidateIntents:
    def test_returns_due_intent(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(conn, id="A", due_at=_iso(now - timedelta(minutes=10)))

        results = _query_candidate_intents(conn, now)
        assert len(results) == 1
        assert results[0]["id"] == "A"

    def test_skips_done_intents(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(conn, id="A", status="done", due_at=_iso(now - timedelta(minutes=10)))

        results = _query_candidate_intents(conn, now)
        assert len(results) == 0

    def test_skips_canceled_intents(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(conn, id="A", status="canceled", due_at=_iso(now - timedelta(minutes=10)))

        results = _query_candidate_intents(conn, now)
        assert len(results) == 0

    def test_skips_intents_due_beyond_window(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        # Due 2 hours in the future — beyond the query window
        _insert_intent(conn, id="A", due_at=_iso(now + timedelta(hours=2)))

        results = _query_candidate_intents(conn, now)
        assert len(results) == 0

    def test_includes_overdue_intent(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        # Due 3 hours ago — overdue (include_overdue behavior)
        _insert_intent(conn, id="A", due_at=_iso(now - timedelta(hours=3)))

        # The SQL uses due_at <= window_end which is "now", so this is included
        # But window_start is now - 1h, and the snooze_until filter uses window_start
        # Actually: the SQL is:
        #   snooze_until IS NULL OR snooze_until <= window_start
        #   AND due_at <= window_end
        # So overdue items are included regardless of lookback window
        results = _query_candidate_intents(conn, now)
        assert len(results) == 1

    def test_includes_event_overlapping_window(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        # Event that started 30 min ago and ends in 30 min
        _insert_intent(
            conn, id="E", type="event",
            start_at=_iso(now - timedelta(minutes=30)),
            end_at=_iso(now + timedelta(minutes=30)),
        )

        results = _query_candidate_intents(conn, now)
        assert len(results) == 1

    def test_skips_snoozed_with_future_snooze(self, tmp_path):
        """Snoozed intents whose snooze_until is still in the future
        are filtered out by the SQL WHERE clause."""
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(
            conn, id="S", status="snoozed",
            due_at=_iso(now - timedelta(minutes=10)),
            snooze_until=_iso(now + timedelta(hours=1)),
        )

        results = _query_candidate_intents(conn, now)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# _filter_and_prepare
# ---------------------------------------------------------------------------

class TestFilterAndPrepare:
    def test_active_due_passes(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        candidate = {
            "id": "A", "status": "active", "priority": 1,
            "due_at": _iso(now - timedelta(minutes=5)),
            "snooze_until": None, "last_fired_at": None,
        }

        result = _filter_and_prepare(conn, [candidate], now)
        assert len(result) == 1

    def test_active_future_due_skipped(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        candidate = {
            "id": "A", "status": "active", "priority": 1,
            "due_at": _iso(now + timedelta(minutes=30)),
            "snooze_until": None, "last_fired_at": None,
        }

        result = _filter_and_prepare(conn, [candidate], now)
        assert len(result) == 0

    def test_snoozed_future_snooze_skipped(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        candidate = {
            "id": "S", "status": "snoozed", "priority": 1,
            "due_at": _iso(now - timedelta(minutes=5)),
            "snooze_until": _iso(now + timedelta(hours=1)),
            "last_fired_at": None,
        }

        result = _filter_and_prepare(conn, [candidate], now)
        assert len(result) == 0

    def test_snoozed_expired_unsnoozes_and_passes(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        past_snooze = _iso(now - timedelta(minutes=10))
        # Insert a real row so the UPDATE works
        _insert_intent(
            conn, id="S", status="snoozed",
            due_at=_iso(now - timedelta(minutes=30)),
            snooze_until=past_snooze,
        )

        candidate = {
            "id": "S", "status": "snoozed", "priority": 1,
            "due_at": _iso(now - timedelta(minutes=30)),
            "snooze_until": past_snooze,
            "last_fired_at": None,
        }

        result = _filter_and_prepare(conn, [candidate], now)
        assert len(result) == 1
        assert result[0]["status"] == "active"
        assert result[0]["snooze_until"] is None

        # Verify DB was actually updated
        row = conn.execute("SELECT status, snooze_until FROM intents WHERE id = 'S'").fetchone()
        assert row["status"] == "active"
        assert row["snooze_until"] is None

    def test_recently_fired_skipped(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        candidate = {
            "id": "R", "status": "active", "priority": 1,
            "due_at": _iso(now - timedelta(minutes=5)),
            "snooze_until": None,
            "last_fired_at": _iso(now - timedelta(minutes=30)),
        }

        result = _filter_and_prepare(conn, [candidate], now)
        assert len(result) == 0

    def test_fired_long_ago_passes(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        candidate = {
            "id": "R", "status": "active", "priority": 1,
            "due_at": _iso(now - timedelta(minutes=5)),
            "snooze_until": None,
            "last_fired_at": _iso(now - timedelta(hours=3)),
        }

        result = _filter_and_prepare(conn, [candidate], now)
        assert len(result) == 1

    def test_fired_exactly_at_cutoff_skipped(self, tmp_path):
        """Fired exactly 2 hours ago — at the boundary, should be skipped."""
        conn = _setup_db(tmp_path)
        now = _now()
        candidate = {
            "id": "R", "status": "active", "priority": 1,
            "due_at": _iso(now - timedelta(minutes=5)),
            "snooze_until": None,
            "last_fired_at": _iso(now - RECENTLY_FIRED_WINDOW),
        }

        result = _filter_and_prepare(conn, [candidate], now)
        # At exactly the cutoff: last_fired_at == fired_cutoff → not < cutoff → skipped
        assert len(result) == 0

    def test_snoozed_without_snooze_until_skipped(self, tmp_path):
        """Snoozed intent with no snooze_until is an anomalous state — skip it."""
        conn = _setup_db(tmp_path)
        now = _now()
        candidate = {
            "id": "SNO", "status": "snoozed", "priority": 1,
            "due_at": _iso(now - timedelta(minutes=5)),
            "snooze_until": None,
            "last_fired_at": None,
        }

        result = _filter_and_prepare(conn, [candidate], now)
        assert len(result) == 0

    def test_mixed_candidates(self, tmp_path):
        """Multiple candidates with different dispositions."""
        conn = _setup_db(tmp_path)
        now = _now()

        # Due and qualifies
        _insert_intent(conn, id="GOOD", due_at=_iso(now - timedelta(minutes=5)))
        good = {
            "id": "GOOD", "status": "active", "priority": 1,
            "due_at": _iso(now - timedelta(minutes=5)),
            "snooze_until": None, "last_fired_at": None,
        }

        # Recently fired — should be filtered
        recently_fired = {
            "id": "FIRED", "status": "active", "priority": 1,
            "due_at": _iso(now - timedelta(minutes=5)),
            "snooze_until": None,
            "last_fired_at": _iso(now - timedelta(minutes=10)),
        }

        # Future due — should be filtered
        future = {
            "id": "FUTURE", "status": "active", "priority": 1,
            "due_at": _iso(now + timedelta(minutes=30)),
            "snooze_until": None, "last_fired_at": None,
        }

        result = _filter_and_prepare(conn, [good, recently_fired, future], now)
        assert len(result) == 1
        assert result[0]["id"] == "GOOD"


# ---------------------------------------------------------------------------
# _format_notification
# ---------------------------------------------------------------------------

class TestFormatNotification:
    def test_single_reminder(self):
        intent = {
            "type": "reminder", "title": "Call dentist",
            "due_at": "2026-04-13T14:00:00Z", "start_at": None,
            "body": None, "priority": 1,
        }
        msg = _format_notification([intent])
        assert "[Reminder]" in msg
        assert "Call dentist" in msg
        assert "2026-04-13 14:00 UTC" in msg

    def test_single_with_body(self):
        intent = {
            "type": "task", "title": "Deploy v2",
            "due_at": "2026-04-13T14:00:00Z", "start_at": None,
            "body": "Run the migration first", "priority": 1,
        }
        msg = _format_notification([intent])
        assert "Run the migration first" in msg

    def test_single_high_priority(self):
        intent = {
            "type": "task", "title": "Fix prod",
            "due_at": "2026-04-13T14:00:00Z", "start_at": None,
            "body": None, "priority": 2,
        }
        msg = _format_notification([intent])
        assert "(high priority)" in msg

    def test_single_urgent_priority(self):
        intent = {
            "type": "task", "title": "Server down",
            "due_at": "2026-04-13T14:00:00Z", "start_at": None,
            "body": None, "priority": 3,
        }
        msg = _format_notification([intent])
        assert "(urgent)" in msg

    def test_multiple_intents(self):
        intents = [
            {
                "type": "reminder", "title": "Call dentist",
                "due_at": "2026-04-13T14:00:00Z", "start_at": None,
                "body": None, "priority": 1,
            },
            {
                "type": "task", "title": "Deploy v2",
                "due_at": "2026-04-13T15:00:00Z", "start_at": None,
                "body": None, "priority": 2,
            },
        ]
        msg = _format_notification(intents)
        assert "You have 2 due items:" in msg
        assert "- [Reminder] Call dentist" in msg
        assert "- [Task] Deploy v2 (high priority)" in msg

    def test_event_type(self):
        intent = {
            "type": "event", "title": "Team standup",
            "due_at": None, "start_at": "2026-04-13T09:00:00Z",
            "body": None, "priority": 0,
        }
        msg = _format_notification([intent])
        assert "[Event]" in msg
        assert "2026-04-13 09:00 UTC" in msg

    def test_followup_type(self):
        intent = {
            "type": "followup", "title": "Check PR status",
            "due_at": "2026-04-13T16:00:00Z", "start_at": None,
            "body": None, "priority": 1,
        }
        msg = _format_notification([intent])
        assert "[Follow-up]" in msg

    def test_no_date(self):
        intent = {
            "type": "task", "title": "Something",
            "due_at": None, "start_at": None,
            "body": None, "priority": 1,
        }
        msg = _format_notification([intent])
        assert "no date" in msg


# ---------------------------------------------------------------------------
# _mark_fired
# ---------------------------------------------------------------------------

class TestMarkFired:
    def test_updates_last_fired_at(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(conn, id="MF1", due_at=_iso(now - timedelta(minutes=5)))

        intent = {"id": "MF1"}
        _mark_fired(conn, [intent], now)

        row = conn.execute("SELECT last_fired_at FROM intents WHERE id = 'MF1'").fetchone()
        assert row["last_fired_at"] == _iso(now)

    def test_updates_multiple_intents(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(conn, id="MF1", due_at=_iso(now - timedelta(minutes=5)))
        _insert_intent(conn, id="MF2", due_at=_iso(now - timedelta(minutes=10)))

        _mark_fired(conn, [{"id": "MF1"}, {"id": "MF2"}], now)

        for iid in ("MF1", "MF2"):
            row = conn.execute(
                "SELECT last_fired_at FROM intents WHERE id = ?", (iid,)
            ).fetchone()
            assert row["last_fired_at"] == _iso(now)

    def test_updates_updated_at(self, tmp_path):
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(conn, id="MF1", due_at=_iso(now - timedelta(minutes=5)))

        _mark_fired(conn, [{"id": "MF1"}], now)

        row = conn.execute("SELECT updated_at FROM intents WHERE id = 'MF1'").fetchone()
        assert row["updated_at"] == _iso(now)


# ---------------------------------------------------------------------------
# check_and_notify (full integration)
# ---------------------------------------------------------------------------

class TestCheckAndNotify:
    @pytest.mark.asyncio
    async def test_no_db_returns_none(self, tmp_path):
        """When intents.db doesn't exist, return None without error."""
        bus = FakeBus()
        result = await check_and_notify(tmp_path, "telegram", "123", bus)
        assert result is None
        assert len(bus.messages) == 0

    @pytest.mark.asyncio
    async def test_no_due_intents(self, tmp_path):
        """With an empty DB, return None."""
        _setup_db(tmp_path)
        bus = FakeBus()
        result = await check_and_notify(tmp_path, "telegram", "123", bus)
        assert result is None
        assert len(bus.messages) == 0

    @pytest.mark.asyncio
    async def test_due_intent_sends_notification(self, tmp_path):
        """A due intent triggers a notification via the bus."""
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(
            conn, id="DUE1", title="Pay rent",
            due_at=_iso(now - timedelta(minutes=10)),
        )
        # Close the test connection so the notifier gets its own
        conn.close()
        # Clear the module-level cache so it creates a fresh connection
        from hazel.agent.tools.intents import _db_cache
        _db_cache.clear()

        bus = FakeBus()
        result = await check_and_notify(tmp_path, "telegram", "42", bus, _now=now)

        assert result is not None
        assert "Pay rent" in result
        assert len(bus.messages) == 1
        assert bus.messages[0].channel == "telegram"
        assert bus.messages[0].chat_id == "42"
        assert "Pay rent" in bus.messages[0].content

    @pytest.mark.asyncio
    async def test_recently_fired_not_resent(self, tmp_path):
        """Intents fired within the 2h window are not re-notified."""
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(
            conn, id="RF1", title="Already notified",
            due_at=_iso(now - timedelta(minutes=10)),
            last_fired_at=_iso(now - timedelta(minutes=30)),
        )
        conn.close()
        from hazel.agent.tools.intents import _db_cache
        _db_cache.clear()

        bus = FakeBus()
        result = await check_and_notify(tmp_path, "telegram", "42", bus, _now=now)

        assert result is None
        assert len(bus.messages) == 0

    @pytest.mark.asyncio
    async def test_expired_snooze_unsnoozed_and_notified(self, tmp_path):
        """Snoozed intent with expired snooze gets un-snoozed and notified."""
        conn = _setup_db(tmp_path)
        now = _now()
        # Snoozed, but snooze expired 10 min ago
        # Note: for the SQL query to return this, snooze_until must be <= window_start
        # window_start = now - 1h, so snooze_until must be at most now - 1h
        _insert_intent(
            conn, id="SN1", title="Snoozed task", status="snoozed",
            due_at=_iso(now - timedelta(minutes=30)),
            snooze_until=_iso(now - timedelta(hours=1, minutes=5)),
        )
        conn.close()
        from hazel.agent.tools.intents import _db_cache
        _db_cache.clear()

        bus = FakeBus()
        result = await check_and_notify(tmp_path, "telegram", "42", bus, _now=now)

        assert result is not None
        assert "Snoozed task" in result

        # Verify DB state: intent should be active now
        fresh_conn = sqlite3.connect(str(tmp_path / "data" / "intents.db"))
        fresh_conn.row_factory = sqlite3.Row
        row = fresh_conn.execute("SELECT status, snooze_until FROM intents WHERE id = 'SN1'").fetchone()
        assert row["status"] == "active"
        assert row["snooze_until"] is None
        fresh_conn.close()

    @pytest.mark.asyncio
    async def test_future_due_not_notified(self, tmp_path):
        """Intents due in the future are not notified."""
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(
            conn, id="FUT1", title="Future task",
            due_at=_iso(now + timedelta(hours=2)),
        )
        conn.close()
        from hazel.agent.tools.intents import _db_cache
        _db_cache.clear()

        bus = FakeBus()
        result = await check_and_notify(tmp_path, "telegram", "42", bus, _now=now)

        assert result is None
        assert len(bus.messages) == 0

    @pytest.mark.asyncio
    async def test_last_fired_at_updated_after_notification(self, tmp_path):
        """After notification, last_fired_at is set on the intent."""
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(
            conn, id="UP1", title="Update check",
            due_at=_iso(now - timedelta(minutes=10)),
        )
        conn.close()
        from hazel.agent.tools.intents import _db_cache
        _db_cache.clear()

        bus = FakeBus()
        await check_and_notify(tmp_path, "telegram", "42", bus, _now=now)

        fresh_conn = sqlite3.connect(str(tmp_path / "data" / "intents.db"))
        fresh_conn.row_factory = sqlite3.Row
        row = fresh_conn.execute("SELECT last_fired_at FROM intents WHERE id = 'UP1'").fetchone()
        assert row["last_fired_at"] == _iso(now)
        fresh_conn.close()

    @pytest.mark.asyncio
    async def test_multiple_due_intents_single_message(self, tmp_path):
        """Multiple due intents produce one aggregated message."""
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(
            conn, id="M1", title="Task one", type="task",
            due_at=_iso(now - timedelta(minutes=5)),
        )
        _insert_intent(
            conn, id="M2", title="Task two", type="reminder",
            due_at=_iso(now - timedelta(minutes=15)),
        )
        conn.close()
        from hazel.agent.tools.intents import _db_cache
        _db_cache.clear()

        bus = FakeBus()
        result = await check_and_notify(tmp_path, "telegram", "42", bus, _now=now)

        assert result is not None
        assert "2 due items" in result
        assert "Task one" in result
        assert "Task two" in result
        # Only one message sent
        assert len(bus.messages) == 1

    @pytest.mark.asyncio
    async def test_still_snoozed_skipped(self, tmp_path):
        """Snoozed intents with future snooze_until are fully skipped."""
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(
            conn, id="SS1", title="Still snoozed", status="snoozed",
            due_at=_iso(now - timedelta(minutes=10)),
            snooze_until=_iso(now + timedelta(hours=1)),
        )
        conn.close()
        from hazel.agent.tools.intents import _db_cache
        _db_cache.clear()

        bus = FakeBus()
        result = await check_and_notify(tmp_path, "telegram", "42", bus, _now=now)

        assert result is None
        assert len(bus.messages) == 0

    @pytest.mark.asyncio
    async def test_done_intents_skipped(self, tmp_path):
        """Done intents are excluded by the SQL query."""
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(
            conn, id="D1", title="Done task", status="done",
            due_at=_iso(now - timedelta(minutes=10)),
        )
        conn.close()
        from hazel.agent.tools.intents import _db_cache
        _db_cache.clear()

        bus = FakeBus()
        result = await check_and_notify(tmp_path, "telegram", "42", bus, _now=now)

        assert result is None

    @pytest.mark.asyncio
    async def test_canceled_intents_skipped(self, tmp_path):
        """Canceled intents are excluded by the SQL query."""
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(
            conn, id="C1", title="Canceled task", status="canceled",
            due_at=_iso(now - timedelta(minutes=10)),
        )
        conn.close()
        from hazel.agent.tools.intents import _db_cache
        _db_cache.clear()

        bus = FakeBus()
        result = await check_and_notify(tmp_path, "telegram", "42", bus, _now=now)

        assert result is None

    @pytest.mark.asyncio
    async def test_channel_and_chat_id_passed_through(self, tmp_path):
        """The channel and chat_id are correctly propagated to the outbound message."""
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(conn, id="CH1", title="Test", due_at=_iso(now - timedelta(minutes=5)))
        conn.close()
        from hazel.agent.tools.intents import _db_cache
        _db_cache.clear()

        bus = FakeBus()
        await check_and_notify(tmp_path, "discord", "guild-123", bus, _now=now)

        assert len(bus.messages) == 1
        assert bus.messages[0].channel == "discord"
        assert bus.messages[0].chat_id == "guild-123"

    @pytest.mark.asyncio
    async def test_second_run_within_window_skips(self, tmp_path):
        """Running the notifier twice within 2h doesn't re-notify."""
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(
            conn, id="TW1", title="Twice check",
            due_at=_iso(now - timedelta(minutes=10)),
        )
        conn.close()
        from hazel.agent.tools.intents import _db_cache
        _db_cache.clear()

        bus = FakeBus()

        # First run: should notify
        result1 = await check_and_notify(tmp_path, "telegram", "42", bus, _now=now)
        assert result1 is not None
        assert len(bus.messages) == 1

        # Second run 5 minutes later: should NOT notify (last_fired_at was set)
        later = now + timedelta(minutes=5)
        result2 = await check_and_notify(tmp_path, "telegram", "42", bus, _now=later)
        assert result2 is None
        assert len(bus.messages) == 1  # still just the one from before

    @pytest.mark.asyncio
    async def test_renotify_after_window_expires(self, tmp_path):
        """After the 2h window, the same intent can be re-notified."""
        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(
            conn, id="RN1", title="Renotify me",
            due_at=_iso(now - timedelta(minutes=10)),
        )
        conn.close()
        from hazel.agent.tools.intents import _db_cache
        _db_cache.clear()

        bus = FakeBus()

        # First run
        await check_and_notify(tmp_path, "telegram", "42", bus, _now=now)
        assert len(bus.messages) == 1

        # Run 2.5 hours later — past the 2h window
        much_later = now + timedelta(hours=2, minutes=30)
        result = await check_and_notify(tmp_path, "telegram", "42", bus, _now=much_later)
        assert result is not None
        assert len(bus.messages) == 2


# ---------------------------------------------------------------------------
# System-event dispatch integration
# ---------------------------------------------------------------------------

class TestSystemEventDispatch:
    @pytest.mark.asyncio
    async def test_handle_system_event_intent_notifications(self, tmp_path):
        """_handle_system_event dispatches to check_and_notify for intent notifications."""
        from hazel.cron.service import INTENT_NOTIFICATIONS_NAME
        from hazel.cron.types import CronJob, CronPayload, CronSchedule

        conn = _setup_db(tmp_path)
        now = _now()
        _insert_intent(
            conn, id="SE1", title="System event test",
            due_at=_iso(now - timedelta(minutes=5)),
        )
        conn.close()
        from hazel.agent.tools.intents import _db_cache
        _db_cache.clear()

        job = CronJob(
            id="test-job",
            name=INTENT_NOTIFICATIONS_NAME,
            payload=CronPayload(
                kind="system_event",
                channel="telegram",
                to="42",
            ),
            schedule=CronSchedule(kind="every", every_ms=300_000),
        )

        bus = FakeBus()

        from hazel.cli.commands import _handle_system_event
        result = await _handle_system_event(job, tmp_path, bus)

        assert result is not None
        assert "System event test" in result

    @pytest.mark.asyncio
    async def test_handle_system_event_unknown_job(self, tmp_path):
        """Unknown system_event jobs return None."""
        from hazel.cron.types import CronJob, CronPayload, CronSchedule

        job = CronJob(
            id="test-job",
            name="Unknown Job",
            payload=CronPayload(kind="system_event"),
            schedule=CronSchedule(kind="every", every_ms=60_000),
        )

        bus = FakeBus()

        from hazel.cli.commands import _handle_system_event
        result = await _handle_system_event(job, tmp_path, bus)

        assert result is None


# ---------------------------------------------------------------------------
# Migration from LLM-based to pure-code
# ---------------------------------------------------------------------------

class TestBootstrapMigration:
    def test_migrates_existing_agent_turn_to_system_event(self, tmp_path):
        """Existing intent notification jobs with kind=agent_turn are upgraded."""
        from hazel.cron.service import (
            CronService,
            INTENT_NOTIFICATIONS_NAME,
            INTENT_NOTIFICATIONS_PROMPT,
        )
        from hazel.cron.types import CronSchedule

        store_path = tmp_path / "cron" / "jobs.json"
        service = CronService(store_path)

        # Simulate old-style job (as it would exist in a pre-upgrade install)
        old_job = service.add_job(
            name=INTENT_NOTIFICATIONS_NAME,
            schedule=CronSchedule(kind="every", every_ms=300_000),
            message="old LLM prompt here",
            deliver=False,
            channel="telegram",
            to="12345",
            kind="agent_turn",  # old style
        )
        assert old_job.payload.kind == "agent_turn"

        # Run bootstrap — should migrate in place
        service.bootstrap_default_jobs()

        migrated = service.get_job(old_job.id)
        assert migrated is not None
        assert migrated.payload.kind == "system_event"
        assert migrated.payload.message == INTENT_NOTIFICATIONS_PROMPT

    def test_no_migration_if_already_system_event(self, tmp_path):
        """Jobs already using system_event are not re-migrated."""
        from hazel.cron.service import (
            CronService,
            INTENT_NOTIFICATIONS_NAME,
            INTENT_NOTIFICATIONS_PROMPT,
        )
        from hazel.cron.types import CronSchedule

        store_path = tmp_path / "cron" / "jobs.json"
        service = CronService(store_path)

        job = service.add_job(
            name=INTENT_NOTIFICATIONS_NAME,
            schedule=CronSchedule(kind="every", every_ms=300_000),
            message=INTENT_NOTIFICATIONS_PROMPT,
            deliver=False,
            channel="telegram",
            to="12345",
            kind="system_event",
        )
        original_updated = job.updated_at_ms

        # Bootstrap should not touch it
        service.bootstrap_default_jobs()

        loaded = service.get_job(job.id)
        assert loaded is not None
        assert loaded.payload.kind == "system_event"
        assert loaded.updated_at_ms == original_updated

    def test_new_install_creates_system_event(self, tmp_path):
        """Fresh installs get a system_event job from bootstrap."""
        from hazel.cron.service import CronService, INTENT_NOTIFICATIONS_NAME

        store_path = tmp_path / "cron" / "jobs.json"
        service = CronService(store_path)

        # Mock a channels config with telegram allow_from
        class TgConfig:
            allow_from = ["99999"]

        class ChannelsCfg:
            telegram = TgConfig()

        service.bootstrap_default_jobs(channels_config=ChannelsCfg())

        jobs = service.list_jobs(include_disabled=True)
        intent_jobs = [j for j in jobs if j.name == INTENT_NOTIFICATIONS_NAME]
        assert len(intent_jobs) == 1
        assert intent_jobs[0].payload.kind == "system_event"
        assert intent_jobs[0].payload.channel == "telegram"
        assert intent_jobs[0].payload.to == "99999"
