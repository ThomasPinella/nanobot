"""Tests for the canvas dashboard server compatibility with Hazel data formats.

Verifies that the dashboard server correctly reads Hazel's:
- Intent SQLite database (intents + intent_links tables)
- Entity files with CARD headers (memory/areas/**/*.md)
- Cards index (memory/_index/_cards.md)
- Change ledger (memory/_index/changes.jsonl)
- Daily memory logs (memory/YYYY-MM-DD.md)
- Long-term memory (memory/MEMORY.md)
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

CANVAS_DIR = Path(__file__).parent.parent / "canvas"
SERVER_SCRIPT = CANVAS_DIR / "dashboard-server.js"
DASHBOARD_HTML = CANVAS_DIR / "dashboard.html"

# Skip the entire module if canvas node_modules aren't installed
_has_node_modules = (CANVAS_DIR / "node_modules" / "better-sqlite3").is_dir()
pytestmark = pytest.mark.skipif(
    not _has_node_modules,
    reason="canvas node_modules not installed (run: cd canvas && npm install)",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path):
    """Create a temporary Hazel workspace with test data."""
    ws = tmp_path / "workspace"

    # Directory structure
    (ws / "memory" / "areas" / "people").mkdir(parents=True)
    (ws / "memory" / "areas" / "projects").mkdir(parents=True)
    (ws / "memory" / "areas" / "domains").mkdir(parents=True)
    (ws / "memory" / "areas" / "places").mkdir(parents=True)
    (ws / "memory" / "areas" / "resources").mkdir(parents=True)
    (ws / "memory" / "areas" / "systems").mkdir(parents=True)
    (ws / "memory" / "_index").mkdir(parents=True)
    (ws / "data").mkdir(parents=True)

    # Entity files
    (ws / "memory" / "areas" / "people" / "alice.md").write_text(
        '<!-- CARD\nid: person_alice\ntype: person\n'
        'gist: Alice is a test person\ntags: ["test", "person"]\n'
        'links:\n  - {rel: works_on, to: project_demo}\n-->\n\n'
        '## Facts (append-only)\n- 2026-03-27: Created for testing\n'
    )
    (ws / "memory" / "areas" / "projects" / "demo.md").write_text(
        '<!-- CARD\nid: project_demo\ntype: project\n'
        'gist: Demo project for testing the dashboard\ntags: ["test", "demo"]\n'
        'links:\n  - {rel: has_member, to: person_alice}\n-->\n\n'
        '## Facts (append-only)\n- 2026-03-27: Created for testing\n'
    )

    # Generate cards index
    cards = "# Entity Cards Index\n\n---\n\n"
    for entity_file in sorted((ws / "memory" / "areas").rglob("*.md")):
        content = entity_file.read_text()
        import re
        card_match = re.search(r"(<!-- CARD.*?-->)", content, re.DOTALL)
        if card_match:
            rel_path = entity_file.relative_to(ws)
            cards += f"## {rel_path}\n\n{card_match.group(1)}\n\n---\n\n"
    (ws / "memory" / "_index" / "_cards.md").write_text(cards)

    # Intent database
    db_path = ws / "data" / "intents.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE intents (
            id TEXT PRIMARY KEY,
            type TEXT CHECK(type IN ('task','reminder','event','followup')) NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            status TEXT DEFAULT 'active',
            priority INTEGER DEFAULT 1,
            estimate_minutes INTEGER,
            timezone TEXT,
            due_at TEXT,
            start_at TEXT,
            end_at TEXT,
            rrule TEXT,
            snooze_until TEXT,
            location_text TEXT,
            attendees_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_fired_at TEXT,
            deferrals INTEGER DEFAULT 0,
            rescheduled_count INTEGER DEFAULT 0
        );
        CREATE TABLE intent_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent_id TEXT NOT NULL,
            entity_id TEXT,
            entity_path TEXT NOT NULL,
            rel TEXT DEFAULT 'relates_to',
            created_at TEXT NOT NULL,
            UNIQUE(intent_id, entity_id, rel),
            FOREIGN KEY(intent_id) REFERENCES intents(id) ON DELETE CASCADE
        );
        CREATE INDEX idx_intents_status ON intents(status);
        CREATE INDEX idx_intents_due ON intents(due_at);
        CREATE INDEX idx_links_entity_id ON intent_links(entity_id);
        CREATE INDEX idx_links_intent ON intent_links(intent_id);
    """)
    conn.executescript("""
        INSERT INTO intents (id, type, title, body, status, priority, due_at, created_at, updated_at)
        VALUES ('01TEST0000000000AAAAAAAA', 'task', 'Test task one', 'A test task body', 'active', 2,
                '2026-04-01T10:00:00Z', '2026-03-27T00:00:00Z', '2026-03-27T00:00:00Z');

        INSERT INTO intents (id, type, title, body, status, priority, start_at, end_at, created_at, updated_at)
        VALUES ('01TEST0000000000BBBBBBBB', 'event', 'Test event', 'An event body', 'active', 1,
                '2026-04-01T14:00:00Z', '2026-04-01T15:00:00Z', '2026-03-27T00:00:00Z', '2026-03-27T00:00:00Z');

        INSERT INTO intents (id, type, title, status, priority, created_at, updated_at)
        VALUES ('01TEST0000000000CCCCCCCC', 'task', 'Done task', 'done', 1,
                '2026-03-20T00:00:00Z', '2026-03-25T00:00:00Z');

        INSERT INTO intent_links (intent_id, entity_id, entity_path, rel, created_at)
        VALUES ('01TEST0000000000AAAAAAAA', 'project_demo', 'memory/areas/projects/demo.md', 'relates_to', '2026-03-27T00:00:00Z');
    """)
    conn.commit()
    conn.close()

    # Change ledger
    changes = [
        {"ts": "2026-03-27T10:00:00Z", "date": "2026-03-27", "op": "create",
         "path": "memory/areas/people/alice.md", "entity_id": "person_alice",
         "entity_type": "person", "reason": "runtime", "summary": "Created Alice entity",
         "tags": ["test"]},
        {"ts": "2026-03-27T10:01:00Z", "date": "2026-03-27", "op": "create",
         "path": "memory/areas/projects/demo.md", "entity_id": "project_demo",
         "entity_type": "project", "reason": "runtime", "summary": "Created demo project",
         "tags": ["test"]},
    ]
    (ws / "memory" / "_index" / "changes.jsonl").write_text(
        "\n".join(json.dumps(c) for c in changes) + "\n"
    )

    # Daily log
    (ws / "memory" / "2026-03-27.md").write_text(
        "# 2026-03-27\n\n[2026-03-27 10:00] Test daily log entry.\n"
    )

    # Long-term memory
    (ws / "memory" / "MEMORY.md").write_text(
        "# Long-term Memory\n\n## Test Section\n- Test fact\n"
    )

    return ws


@pytest.fixture()
def server(workspace):
    """Start the dashboard server against the test workspace, yield its base URL."""
    port = 18199  # unlikely to collide
    env = os.environ.copy()
    env["HAZEL_WORKSPACE"] = str(workspace)
    env["DASHBOARD_PORT"] = str(port)

    proc = subprocess.Popen(
        ["node", str(SERVER_SCRIPT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(CANVAS_DIR),
    )

    base_url = f"http://localhost:{port}"

    # Wait for server to be ready
    for _ in range(30):
        try:
            urllib.request.urlopen(f"{base_url}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        out = proc.stdout.read().decode() if proc.stdout else ""
        pytest.fail(f"Server did not start in time. Output:\n{out}")

    yield base_url

    proc.terminate()
    proc.wait(timeout=5)


def _get(url):
    """Fetch URL and return parsed JSON."""
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def _get_text(url):
    """Fetch URL and return text."""
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read().decode()


# ---------------------------------------------------------------------------
# Tests: Static files
# ---------------------------------------------------------------------------


class TestStaticFiles:
    """Dashboard HTML and workspace file serving."""

    def test_dashboard_html_served_at_root(self, server):
        html = _get_text(f"{server}/")
        assert "<title>Hazel Canvas</title>" in html

    def test_cards_index_served(self, server):
        text = _get_text(f"{server}/memory/_index/_cards.md")
        assert "person_alice" in text
        assert "project_demo" in text

    def test_entity_file_served(self, server):
        text = _get_text(f"{server}/memory/areas/people/alice.md")
        assert "person_alice" in text
        assert "gist: Alice is a test person" in text

    def test_daily_log_served(self, server):
        text = _get_text(f"{server}/memory/2026-03-27.md")
        assert "Test daily log entry" in text

    def test_memory_md_served(self, server):
        text = _get_text(f"{server}/memory/MEMORY.md")
        assert "Long-term Memory" in text

    def test_404_for_nonexistent(self, server):
        try:
            urllib.request.urlopen(f"{server}/nonexistent", timeout=5)
            pytest.fail("Expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404


# ---------------------------------------------------------------------------
# Tests: Health endpoint
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_ok(self, server):
        data = _get(f"{server}/health")
        assert data["status"] == "ok"
        assert data["database"] == "connected"
        assert "workspace" in data


# ---------------------------------------------------------------------------
# Tests: Intents API
# ---------------------------------------------------------------------------


class TestIntentsApi:
    def test_list_all_intents(self, server):
        data = _get(f"{server}/api/intents")
        assert data["count"] == 3
        ids = {i["id"] for i in data["intents"]}
        assert "01TEST0000000000AAAAAAAA" in ids
        assert "01TEST0000000000BBBBBBBB" in ids
        assert "01TEST0000000000CCCCCCCC" in ids

    def test_filter_by_status(self, server):
        data = _get(f"{server}/api/intents?status=active")
        assert all(i["status"] == "active" for i in data["intents"])
        assert data["count"] == 2

    def test_filter_by_type(self, server):
        data = _get(f"{server}/api/intents?type=event")
        assert data["count"] == 1
        assert data["intents"][0]["type"] == "event"

    def test_search_by_query(self, server):
        data = _get(f"{server}/api/intents?q=task+one")
        assert data["count"] == 1
        assert data["intents"][0]["title"] == "Test task one"

    def test_linked_paths_parsed(self, server):
        data = _get(f"{server}/api/intents")
        task = next(i for i in data["intents"] if i["id"] == "01TEST0000000000AAAAAAAA")
        assert "memory/areas/projects/demo.md" in task["linked_paths"]
        assert "project_demo" in task["linked_ids"]

    def test_single_intent_with_links(self, server):
        data = _get(f"{server}/api/intents/01TEST0000000000AAAAAAAA")
        assert data["intent"]["title"] == "Test task one"
        assert len(data["links"]) == 1
        assert data["links"][0]["entity_id"] == "project_demo"

    def test_single_intent_404(self, server):
        try:
            _get(f"{server}/api/intents/NONEXISTENT")
            pytest.fail("Expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404


# ---------------------------------------------------------------------------
# Tests: Stats API
# ---------------------------------------------------------------------------


class TestStatsApi:
    def test_stats_counts(self, server):
        data = _get(f"{server}/api/stats")
        assert isinstance(data["stats"], list)
        total = sum(s["count"] for s in data["stats"])
        assert total == 3
        assert isinstance(data["overdue"], int)
        assert isinstance(data["upcoming"], int)


# ---------------------------------------------------------------------------
# Tests: Changes API
# ---------------------------------------------------------------------------


class TestChangesApi:
    def test_list_changes(self, server):
        data = _get(f"{server}/api/changes")
        assert data["count"] == 2
        ids = {c["entity_id"] for c in data["changes"]}
        assert "person_alice" in ids
        assert "project_demo" in ids

    def test_filter_by_entity_type(self, server):
        data = _get(f"{server}/api/changes?entity_type=person")
        assert data["count"] == 1
        assert data["changes"][0]["entity_id"] == "person_alice"

    def test_filter_by_entity_id(self, server):
        data = _get(f"{server}/api/changes?entity_id=project_demo")
        assert data["count"] == 1


# ---------------------------------------------------------------------------
# Tests: Memory daily API
# ---------------------------------------------------------------------------


class TestMemoryDailyApi:
    def test_list_daily_logs(self, server):
        data = _get(f"{server}/api/memory/daily")
        assert "2026-03-27" in data["dates"]


# ---------------------------------------------------------------------------
# Tests: Dashboard HTML content
# ---------------------------------------------------------------------------


class TestDashboardHtml:
    """Verify the dashboard HTML has correct Hazel entity types."""

    def test_contains_hazel_entity_types(self):
        html = DASHBOARD_HTML.read_text()
        # Should have all 6 Hazel entity types
        for etype in ["person", "place", "project", "domain", "resource", "system"]:
            assert f'data-type="{etype}"' in html, f"Missing entity type filter: {etype}"

    def test_does_not_contain_old_types(self):
        html = DASHBOARD_HTML.read_text()
        # Should NOT have old non-Hazel types
        assert 'data-type="company"' not in html
        assert 'data-type="topic"' not in html

    def test_type_colors_match_entity_types(self):
        html = DASHBOARD_HTML.read_text()
        for etype in ["person", "place", "project", "domain", "resource", "system"]:
            assert f"{etype}:" in html, f"Missing typeColor for: {etype}"

    def test_title_is_hazel(self):
        html = DASHBOARD_HTML.read_text()
        assert "<title>Hazel Canvas</title>" in html

    def test_memory_path_correct(self):
        html = DASHBOARD_HTML.read_text()
        assert "/memory/MEMORY.md" in html
        # Should NOT fetch from root-level MEMORY.md
        assert "fetch(`${API_BASE}/MEMORY.md`)" not in html

    def test_no_hardcoded_entity_ids(self):
        html = DASHBOARD_HTML.read_text()
        assert "person_thomas" not in html
        assert "project_hazel" not in html
