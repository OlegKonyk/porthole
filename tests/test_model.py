from __future__ import annotations

import json
from datetime import UTC, datetime

from porthole.model import (
    RUN_STATES,
    Box,
    Event,
    Status,
    fmt_age,
    fmt_cost,
    fmt_elapsed,
    parse_iso,
    run_state_style,
)

from .conftest import FIXTURES


def test_status_parses_fixture_and_sorts_running_runs_first() -> None:
    status = Status.from_json(json.loads((FIXTURES / "status.json").read_text()))
    assert [b.name for b in status.sorted_boxes] == [
        "zeta-tests",
        "mid-api",
        "omega-web",
        "alpha-docs",
    ]
    assert status.running_runs == 1
    omega = status.sorted_boxes[2]
    assert omega.run is not None and omega.run.state == "lost"
    assert not omega.has_running_run and omega.is_running
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


def test_lost_and_unknown_runs_are_not_running() -> None:
    base = {"name": "b", "instance": "agent-box-b", "repo": "/r", "state": "running"}
    for state in ("lost", "unknown", "done", "failed", "stopped"):
        box = Box.from_json({**base, "run": {"id": "R", "state": state}, "runs_total": 1})
        assert not box.has_running_run
        assert box.sort_key == (1, "b")
    running = Box.from_json({**base, "run": {"id": "R", "state": "running"}, "runs_total": 1})
    assert running.sort_key == (0, "b")
    assert run_state_style("lost") == run_state_style("failed") == "red"
    assert run_state_style("unknown") == "dim"
    assert run_state_style("running") == run_state_style("done") == ""
    assert "lost" in RUN_STATES and "unknown" in RUN_STATES


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


def test_render_event_shows_tool_argument_from_detail():
    from porthole.app import render_event
    from porthole.model import Event

    cli_shape = Event(
        ts="2026-09-05T20:32:38Z",
        run="r",
        kind="tool",
        text="Bash",
        tool="Bash",
        detail="git log --oneline -3",
    )
    assert "Bash  git log --oneline -3" in render_event(cli_shape).plain
    assert "Bash  Bash" not in render_event(cli_shape).plain
    old_shape = Event(
        ts="2026-09-05T20:32:38Z", run="r", kind="tool", text="src/x.py", tool="Read", detail=None
    )
    assert "Read  src/x.py" in render_event(old_shape).plain
