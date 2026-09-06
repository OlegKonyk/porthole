"""Pilot tests against tests/fake-agentbox (AGENTBOX points at it) and the fixtures."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path

import pytest
from textual.widgets import DataTable, RichLog, Static

from porthole.app import ConfirmScreen, PortholeApp, RunsScreen
from porthole.backend import FixtureBackend, ProcessLogFollower
from porthole.cli import build_app

from .conftest import FIXTURES, calls

EXPECTED_ORDER = ["zeta-tests", "mid-api", "omega-web", "alpha-docs"]  # lost run: not running


async def wait_for(condition, timeout: float = 5.0, step: float = 0.05) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(step)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def grandchild_pid(pidfile: Path) -> int:
    for line in pidfile.read_text().splitlines():
        label, pid = line.split()
        if label == "child":
            return int(pid)
    raise AssertionError("the fake did not record a grandchild pid")


def table_names(app: PortholeApp) -> list[str]:
    table = app.query_one("#boxes", DataTable)
    return [str(table.get_row_at(i)[2]) for i in range(table.row_count)]


async def wait_for_table(app: PortholeApp) -> None:
    await wait_for(lambda: app.query_one("#boxes", DataTable).row_count == 4)


async def test_table_renders_fixture_boxes_in_order(fake_log: Path) -> None:
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        assert table_names(app) == EXPECTED_ORDER
        table = app.query_one("#boxes", DataTable)
        zeta = table.get_row_at(0)
        assert str(zeta[1]) == "deny" and zeta[1].style == "dim"
        assert [str(c) for c in zeta[2:]] == [
            "zeta-tests",
            "running",
            "07:07",
            "12",
            "$0.43",
            "Edit  tests/e2e/checkout.spec.ts",
        ]
        omega = table.get_row_at(2)
        assert str(omega[3]) == "lost" and omega[3].style == "red"
        egress = [(str(table.get_row_at(i)[1]), table.get_row_at(i)[1].style) for i in range(4)]
        assert egress == [
            ("deny", "dim"),
            ("observe", "yellow"),
            ("open", "red"),
            ("unknown", "dim"),
        ]
        alpha_last = table.get_row_at(3)[7]  # stopped box: no run, but the CLI said why
        assert alpha_last.plain == "egress: box is stopped; no live ruleset to read"
        assert alpha_last.spans and alpha_last.spans[0].style == "dim"
        assert app.error is None  # a stopped box's detail is not a header error
        summary = str(app.query_one("#summary", Static).content)
        assert "4 boxes" in summary
        assert "1 running run" in summary
        assert "ago" in summary
        assert app.query_one("#error", Static).has_class("hidden")
    assert [c[0] for c in calls(fake_log)] == ["status", "logs"]


async def test_selecting_running_box_streams_its_log(fake_log: Path) -> None:
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        assert app.selected == "zeta-tests"
        await wait_for(lambda: app.log_lines >= 13)  # banner + 12 fixture events
        log = app.query_one("#log", RichLog)
        rendered = "\n".join(str(line) for line in log.lines)
        assert "run started: sonnet" in rendered
        assert "Edit" in rendered
        assert "12 turns, $0.43" in rendered
    logs_calls = [c for c in calls(fake_log) if c[0] == "logs"]
    assert logs_calls == [
        ["logs", "-f", "--json", "--", "/home/me/dev/zeta-tests", "20260905-101500"]
    ]


async def test_stop_then_confirm_calls_stop_run(fake_log: Path) -> None:
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await wait_for(lambda: any(c[0] == "stop-run" for c in calls(fake_log)))
        await app.workers.wait_for_complete()
    stops = [c for c in calls(fake_log) if c[0] == "stop-run"]
    assert stops == [["stop-run", "--", "/home/me/dev/zeta-tests", "20260905-101500"]]


async def test_stop_then_cancel_does_not_call_stop_run(fake_log: Path) -> None:
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)
    assert not any(c[0] == "stop-run" for c in calls(fake_log))


async def test_changing_selection_kills_previous_follow_process(
    fake_log: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FAKE_AGENTBOX_GAP", "5")  # keep the child alive until we kill it
    monkeypatch.setenv("FAKE_AGENTBOX_GRANDCHILD", "1")  # the shape of the real bash CLI
    pidfile = tmp_path / "pids"
    monkeypatch.setenv("FAKE_AGENTBOX_PIDFILE", str(pidfile))
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        await wait_for(lambda: isinstance(app.follower, ProcessLogFollower))
        first = app.follower
        assert isinstance(first, ProcessLogFollower)
        await wait_for(lambda: first.process is not None)
        pid = first.process.pid
        assert first.process.returncode is None  # alive, mid-stream
        await pilot.press("j")
        await pilot.pause()
        await wait_for(lambda: app.selected == "mid-api")
        await wait_for(lambda: first.reaped)
        assert first.process.returncode is not None  # terminated and waited for: no zombie
        assert app.follower is None  # mid-api has no runs, so nothing is followed
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # the pid is gone, not lingering as a zombie
        grandchild = grandchild_pid(pidfile)
        await wait_for(lambda: not pid_alive(grandchild))  # the whole group went, not one pid
        log = app.query_one("#log", RichLog)
        assert "no runs yet" in "\n".join(str(line) for line in log.lines)
        await pilot.press("k")
        await pilot.pause()
        await wait_for(lambda: app.selected == "zeta-tests")
        await wait_for(lambda: isinstance(app.follower, ProcessLogFollower))
        assert app.follower is not first
    logs_calls = [c for c in calls(fake_log) if c[0] == "logs"]
    assert len(logs_calls) == 2


async def test_failed_status_keeps_last_data_and_shows_error(
    fake_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        monkeypatch.setenv("FAKE_AGENTBOX_FAIL_STATUS", "1")
        await pilot.press("r")
        await wait_for(lambda: app.error is not None)
        await pilot.pause()
        error = app.query_one("#error", Static)
        assert not error.has_class("hidden")
        assert "status exited 1" in str(error.content)
        assert "connection refused" in str(error.content)
        assert table_names(app) == EXPECTED_ORDER
        assert app.selected == "zeta-tests"
        monkeypatch.delenv("FAKE_AGENTBOX_FAIL_STATUS")
        await pilot.press("r")
        await wait_for(lambda: app.error is None)
        await pilot.pause()
        assert error.has_class("hidden")


async def test_missing_cli_is_an_error_line_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBOX", "/nonexistent/agentbox")
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for(lambda: app.error is not None)
        await pilot.pause()
        assert "cannot run /nonexistent/agentbox" in app.error
        assert app.query_one("#boxes", DataTable).row_count == 0
        assert app.is_running


async def test_enter_opens_runs_modal_and_esc_closes(fake_log: Path) -> None:
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RunsScreen)
        runs_table = app.screen.query_one("#runs", DataTable)
        await wait_for(lambda: runs_table.row_count == 5)
        assert str(runs_table.get_row_at(0)[0]) == "20260905-101500"
        assert str(runs_table.get_row_at(1)[1]) == "done"
        lost, unknown = runs_table.get_row_at(3)[1], runs_table.get_row_at(4)[1]
        assert str(lost) == "lost" and lost.style == "red"
        assert str(unknown) == "unknown" and unknown.style == "dim"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, RunsScreen)
    assert ["runs", "--json", "--", "/home/me/dev/zeta-tests"] in calls(fake_log)


async def test_follow_toggle_and_help(fake_log: Path) -> None:
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        assert app.follow_enabled
        await pilot.press("l")
        await pilot.pause()
        assert not app.follow_enabled
        assert "follow off" in str(app.query_one("#summary", Static).content)
        await pilot.press("l")
        await pilot.pause()
        assert app.follow_enabled
        await pilot.press("question_mark")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "HelpScreen"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ != "HelpScreen"


async def test_attach_in_headless_mode_reports_and_does_not_crash(fake_log: Path) -> None:
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert app.is_running
        assert app.error is not None and "cannot suspend" in app.error
    assert not any(c[0] == "attach" for c in calls(fake_log))


def no_suspend(self: PortholeApp) -> contextlib.AbstractContextManager[None]:
    return contextlib.nullcontext()


async def test_attach_runs_the_cli_with_the_run_session(
    fake_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PortholeApp, "suspend", no_suspend)
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        await pilot.press("a")  # zeta-tests: a running run, no claude session
        await pilot.pause()
        assert app.error is None
        await pilot.press("j")
        await pilot.pause()
        await wait_for(lambda: app.selected == "mid-api")
        await pilot.press("a")  # mid-api: a claude session listed
        await pilot.pause()
        assert app.error is None
        await pilot.press("j")
        await pilot.pause()
        await wait_for(lambda: app.selected == "omega-web")
        await pilot.press("s")  # a lost run is not running: nothing to stop
        await pilot.pause()
        assert app.error == "omega-web has no running run to stop"
        await pilot.press("j")
        await pilot.pause()
        await wait_for(lambda: app.selected == "alpha-docs")
        await pilot.press("a")  # alpha-docs is stopped: refused before any CLI call
        await pilot.pause()
        assert app.error == "alpha-docs is stopped; nothing to attach to"
    attaches = [c for c in calls(fake_log) if c[0] == "attach"]
    assert attaches == [
        ["attach", "--", "/home/me/dev/zeta-tests", "run-20260905-101500"],
        ["attach", "--", "/home/me/dev/mid-api", "claude"],
    ]


async def test_attach_non_zero_exit_is_a_header_error(
    fake_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PortholeApp, "suspend", no_suspend)
    monkeypatch.setenv("FAKE_AGENTBOX_ATTACH_EXIT", "3")
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert app.is_running
        assert app.error is not None and app.error.startswith("attach exited 3")


async def test_attach_missing_cli_is_a_header_error(
    fake_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PortholeApp, "suspend", no_suspend)
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        app.backend.cli = "/nonexistent/agentbox"  # renamed or lost +x after the poll
        await pilot.press("a")
        await pilot.pause()
        assert app.is_running
        assert app.error is not None and app.error.startswith("cannot run /nonexistent/agentbox")


async def test_q_quits(fake_log: Path) -> None:
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
    assert app.return_code == 0
    assert not app.is_running


async def test_fixture_backend_runs_without_a_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBOX", "/nonexistent/agentbox")  # must not be called
    app = build_app(["--fixtures", str(FIXTURES), "--interval", "1"])
    assert isinstance(app.backend, FixtureBackend)
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        assert table_names(app) == EXPECTED_ORDER
        await wait_for(lambda: app.log_lines >= 13)
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("y")
        expected = [("/home/me/dev/zeta-tests", "20260905-101500")]
        await wait_for(lambda: app.backend.stop_calls == expected)
        await pilot.press("a")
        await pilot.pause()
        assert app.error == "attach is unavailable in fixture mode"
        await pilot.press("j")
        await pilot.pause()
        await wait_for(lambda: app.selected == "mid-api")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RunsScreen)
        status = app.screen.query_one("#runs-status", Static)
        await wait_for(lambda: "0 runs" in str(status.content))
        assert app.screen.query_one("#runs", DataTable).row_count == 0
        await pilot.press("escape")


async def test_slow_status_does_not_pile_up_polls(
    fake_log: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FAKE_AGENTBOX_STATUS_SLEEP", "0.5")
    pidfile = tmp_path / "pids"
    monkeypatch.setenv("FAKE_AGENTBOX_PIDFILE", str(pidfile))
    app = build_app(["--interval", "0.1"])
    async with app.run_test() as pilot:
        await asyncio.sleep(1.2)
        await pilot.pause()
        status_calls = [c for c in calls(fake_log) if c[0] == "status"]
        assert 1 <= len(status_calls) <= 3  # not one per 100 ms tick
        assert app.polls_skipped >= 5
        assert app.is_running
        await pilot.press("q")
        await pilot.pause()
    await asyncio.sleep(0.2)
    pids = [int(line.split()[1]) for line in pidfile.read_text().splitlines()]
    await wait_for(lambda: not any(pid_alive(pid) for pid in pids))  # nothing outlives the app


async def test_bad_status_render_keeps_last_table(
    fake_log: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        bad = json.loads((FIXTURES / "status.json").read_text())
        bad["boxes"][1]["run"]["cost_usd"] = "0.43"  # a string where the contract says float
        (tmp_path / "bad.json").write_text(json.dumps(bad))
        monkeypatch.setenv("FAKE_AGENTBOX_STATUS_FILE", str(tmp_path / "bad.json"))
        await pilot.press("r")
        await wait_for(lambda: app.error is not None)
        await pilot.pause()
        assert app.error.startswith("status render failed:")
        assert table_names(app) == EXPECTED_ORDER
        assert app.status is not None and app.status.running_runs == 1
        dup = json.loads((FIXTURES / "status.json").read_text())
        dup["boxes"][0]["name"] = "zeta-tests"
        (tmp_path / "dup.json").write_text(json.dumps(dup))
        monkeypatch.setenv("FAKE_AGENTBOX_STATUS_FILE", str(tmp_path / "dup.json"))
        app.error = None
        await pilot.press("r")
        await wait_for(lambda: app.error is not None)
        assert "two boxes share a name" in app.error
        assert table_names(app) == EXPECTED_ORDER
        assert app.is_running


async def test_runs_modal_survives_null_runids_and_reports_failures(
    fake_log: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runs = json.loads((FIXTURES / "runs.json").read_text())
    for run in runs:
        run["runid"] = None
    (tmp_path / "runs.json").write_text(json.dumps(runs))
    monkeypatch.setenv("FAKE_AGENTBOX_RUNS_FILE", str(tmp_path / "runs.json"))
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        runs_table = app.screen.query_one("#runs", DataTable)
        await wait_for(lambda: runs_table.row_count == 5)
        assert app.error is None
        await pilot.press("escape")
        await pilot.pause()
        monkeypatch.setenv("FAKE_AGENTBOX_FAIL_RUNS", "1")
        await pilot.press("enter")
        await pilot.pause()
        await wait_for(lambda: app.error is not None)
        assert "runs failed: runs exited 1" in app.error
        status = app.screen.query_one("#runs-status", Static)
        assert "runs failed" in str(status.content)
        await pilot.press("escape")


async def test_long_log_line_is_skipped_and_the_log_continues(
    fake_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_AGENTBOX_LONG_LINE", str(5 * 1024 * 1024))
    monkeypatch.setenv("FAKE_AGENTBOX_GAP", "0.01")
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        await wait_for(lambda: app.log_lines >= 14, timeout=15)  # banner + 12 events + skip line
        log = app.query_one("#log", RichLog)
        rendered = "\n".join(str(line) for line in log.lines)
        assert "was skipped; the log continues" in rendered
        assert "12 turns, $0.43" in rendered  # the last event still arrived
        assert app.error is None


async def test_chatty_stderr_does_not_stall_the_log(
    fake_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_AGENTBOX_STDERR_SPAM", "300")  # ~300 KiB, past any pipe buffer
    monkeypatch.setenv("FAKE_AGENTBOX_GAP", "0.01")
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        await wait_for(lambda: app.log_lines >= 13 + 300, timeout=15)
        log = app.query_one("#log", RichLog)
        rendered = "\n".join(str(line) for line in log.lines)
        assert "12 turns, $0.43" in rendered
        assert "limactl warning 0" in rendered  # rendered through the non-JSON branch


async def test_log_failure_is_visible_in_pane_and_header(
    fake_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        await wait_for(lambda: app.log_lines >= 13)
        app.backend.cli = "/nonexistent/agentbox"
        await pilot.press("j")
        await pilot.pause()
        await pilot.press("k")  # back to zeta-tests: the follow restarts and cannot spawn
        await pilot.pause()
        await wait_for(lambda: app.error is not None)
        assert app.error.startswith("logs: cannot run /nonexistent/agentbox")
        log = app.query_one("#log", RichLog)
        rendered = "\n".join(str(line) for line in log.lines)
        assert "log stopped: cannot run" in rendered
        assert app.follow_failed


async def test_selection_change_during_follower_teardown_wins(
    fake_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll's sync_selection blocked in close() must not reinstall its box's log."""
    monkeypatch.setenv("FAKE_AGENTBOX_GAP", "5")
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        await wait_for(lambda: isinstance(app.follower, ProcessLogFollower))
        first = app.follower

        async def slow_close(original=first.close) -> None:
            await asyncio.sleep(0.3)  # a limactl teardown that takes a while
            await original()

        first.close = slow_close  # type: ignore[method-assign]
        app.follow_key = ("zeta-tests", "R2")  # pretend the poll saw a new run id
        poll = asyncio.ensure_future(app.sync_selection())
        await asyncio.sleep(0.05)
        await pilot.press("j")  # the operator moves on while close() is in flight
        await pilot.pause()
        await poll
        await wait_for(lambda: app.selected == "mid-api" and app.follow_key == ("mid-api", None))
        assert app.follower is None
        log = app.query_one("#log", RichLog)
        rendered = "\n".join(str(line) for line in log.lines)
        assert "mid-api has no runs yet" in rendered
        assert "zeta-tests" not in rendered
    logs_calls = [c for c in calls(fake_log) if c[0] == "logs"]
    assert len(logs_calls) == 1  # no second zeta-tests follower was ever spawned


async def test_command_palette_is_off(fake_log: Path) -> None:
    """The palette's Screenshot command writes to disk; the boundary says nothing does."""
    assert PortholeApp.ENABLE_COMMAND_PALETTE is False
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        assert "ctrl+p" not in app.active_bindings
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert app.screen.__class__.__name__ != "CommandPalette"


async def test_egress_disagreement_is_a_header_error(
    fake_log: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        assert app.error is None
        status = json.loads((FIXTURES / "status.json").read_text())
        zeta = status["boxes"][1]
        zeta["firewall"] = "unknown"
        zeta["firewall_detail"] = "mode file says deny but the live ruleset disagrees (open)"
        (tmp_path / "status.json").write_text(json.dumps(status))
        monkeypatch.setenv("FAKE_AGENTBOX_STATUS_FILE", str(tmp_path / "status.json"))
        await pilot.press("r")
        await wait_for(lambda: app.error is not None)
        await pilot.pause()
        assert app.error == (
            "egress zeta-tests: mode file says deny but the live ruleset disagrees (open)"
        )
        assert app.error_source == "egress"
        table = app.query_one("#boxes", DataTable)
        zeta_last = table.get_row_at(0)[7]
        assert zeta_last.plain.startswith("Edit  tests/e2e/checkout.spec.ts  egress: mode file")
        assert str(table.get_row_at(0)[1]) == "unknown"
        monkeypatch.delenv("FAKE_AGENTBOX_STATUS_FILE")
        await pilot.press("r")
        await wait_for(lambda: app.error is None)  # a clean poll clears it
        assert app.is_running


async def test_poll_rerender_does_not_undo_a_keypress(fake_log: Path) -> None:
    """A poll that re-renders while a cursor move's highlight message is still queued."""
    app = build_app([])
    async with app.run_test() as pilot:
        await wait_for_table(app)
        await pilot.pause()
        table = app.query_one("#boxes", DataTable)
        assert app.status is not None
        table.action_cursor_down()  # the cursor is on mid-api; RowHighlighted is queued
        app.render_table(app.status)  # the poll rebuilds the table before it is handled
        await pilot.pause()
        assert table.cursor_row == 1
        assert app.selected == "mid-api"
        await wait_for(lambda: app.follow_key == ("mid-api", None))
