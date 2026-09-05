from __future__ import annotations

import json
from datetime import UTC, datetime

from porthole.model import Box, Event, Status, fmt_age, fmt_cost, fmt_elapsed, parse_iso

from .conftest import FIXTURES


def test_status_parses_fixture_and_sorts_running_runs_first() -> None:
    status = Status.from_json(json.loads((FIXTURES / "status.json").read_text()))
    assert [b.name for b in status.sorted_boxes] == ["zeta-tests", "mid-api", "alpha-docs"]
    assert status.running_runs == 1
    zeta = status.sorted_boxes[0]
    assert zeta.run is not None and zeta.run.id == "20260905-101500"
    assert zeta.target == "/home/me/dev/zeta-tests"


def test_box_without_repo_targets_its_name() -> None:
    box = Box.from_json({"name": "orphan", "instance": "agent-box-orphan", "repo": None})
    assert box.target == "orphan"
    assert box.state == "stopped"
    assert box.run is None


def test_status_age_from_generated_at() -> None:
    status = Status(generated_at="2026-09-05T10:22:07Z")
    now = datetime(2026, 9, 5, 10, 22, 19, tzinfo=UTC)
    assert status.age_s(now) == 12.0
    assert Status(generated_at=None).age_s(now) is None
    assert Status(generated_at="not a date").age_s(now) is None


def test_event_unknown_kind_falls_back_to_text() -> None:
    event = Event.from_json({"kind": "mystery", "text": "x"})
    assert event.kind == "text"
    assert Event.from_json({"kind": "tool", "tool": "Edit", "text": "a.py"}).tool == "Edit"


def test_formatting() -> None:
    assert fmt_elapsed(427) == "07:07"
    assert fmt_elapsed(3725) == "62:05"
    assert fmt_elapsed(None) == ""
    assert fmt_cost(0.4312) == "$0.43"
    assert fmt_cost(None) == ""
    assert fmt_age(12) == "12s ago"
    assert fmt_age(600) == "10m ago"
    assert fmt_age(7200) == "2.0h ago"
    assert fmt_age(None) == "?"
    assert parse_iso("2026-09-05T10:15:00+00:00") == datetime(2026, 9, 5, 10, 15, tzinfo=UTC)
