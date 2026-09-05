"""The only thing that talks to the outside: the ``agentbox`` CLI, or fixture files.

Both backends expose the same five operations. The CLI backend runs the CLI as a
subprocess and parses its ``--json`` output; the fixture backend replays files so
the app can be tried and tested without a box. Nothing here touches ``limactl``,
guest files, transcripts, or the network.

Process hygiene, because the real CLI is a shell wrapper around ``limactl``:
every child is started in its own session (so it leads a process group), a
stuck call is killed by group and waited for, and cancellation never abandons
a child.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from .model import Box, Event, Status

DEFAULT_CLI = "agentbox"
DEFAULT_TIMEOUT_S = 10.0
LOG_LINE_LIMIT = 4 * 1024 * 1024  # bytes; one event line longer than this is skipped
NON_JSON_LINE_LIMIT = 500  # characters of a non-JSON line shown in the pane
GROUP_KILL_GRACE_S = 5.0


class BackendError(Exception):
    """A CLI call failed. The message is one line, fit for the header."""


class LogFollower(Protocol):
    """A stream of events for one run, closeable at any point."""

    def __aiter__(self) -> AsyncIterator[Event]: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...


class Backend(Protocol):
    async def status(self) -> Status: ...

    async def runs(self, target: str) -> list[dict[str, Any]]: ...

    def follow(self, target: str, runid: str | None) -> LogFollower: ...

    async def stop_run(self, target: str, runid: str | None) -> None: ...

    def attach_argv(self, target: str, session: str | None) -> list[str] | None: ...


def _one_line(text: str, limit: int = 160) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:limit]


def check_operand(value: str, what: str) -> str:
    """Operands come from the CLI's own JSON; never let one be read back as an option."""
    if not value:
        raise BackendError(f"empty {what}")
    if value.startswith("-"):
        raise BackendError(f"refusing {what} that starts with '-': {value[:40]}")
    return value


async def kill_group(
    process: asyncio.subprocess.Process, grace_s: float = GROUP_KILL_GRACE_S
) -> None:
    """SIGTERM the child's whole process group, wait, then SIGKILL it. Always reaps."""
    if process.returncode is not None:
        return
    _signal_group(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_s)
        return
    except TimeoutError:
        pass
    _signal_group(process, signal.SIGKILL)
    await process.wait()


def _signal_group(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)  # start_new_session=True made pid the group id
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            pass


# --------------------------------------------------------------------------- CLI


class ProcessLogFollower:
    """Runs ``agentbox logs -f --json -- <target> [runid]`` and yields one Event per line.

    ``start`` spawns the child so its handle exists before any cancellable read;
    ``close`` kills the child's process group and waits, so no zombie and no
    orphaned ``limactl`` is left behind. stderr is merged into stdout so CLI
    warnings render as plain lines instead of filling an undrained pipe.
    """

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.process: asyncio.subprocess.Process | None = None
        self._closed = False

    async def start(self) -> None:
        if self.process is not None or self._closed:
            return
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=LOG_LINE_LIMIT,
                start_new_session=True,
            )
        except OSError as exc:
            self._closed = True
            raise BackendError(f"cannot run {self.argv[0]}: {exc}") from exc

    async def __aiter__(self) -> AsyncIterator[Event]:
        await self.start()
        process = self.process
        if process is None:
            return
        assert process.stdout is not None
        try:
            while not self._closed:
                try:
                    raw = await process.stdout.readline()
                except ValueError:
                    # asyncio raises ValueError (after LimitOverrunError) for a line
                    # over the limit and discards what it buffered; the stream goes on.
                    yield Event(
                        ts=None,
                        run=None,
                        kind="status",
                        text=f"one log line over {LOG_LINE_LIMIT // (1024 * 1024)} MiB was "
                        "skipped; the log continues",
                    )
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    yield Event(ts=None, run=None, kind="status", text=line[:NON_JSON_LINE_LIMIT])
                    continue
                if isinstance(data, dict):
                    yield Event.from_json(data)
            if not self._closed:
                code = await process.wait()
                if code != 0:
                    raise BackendError(f"logs exited {code}")
        finally:
            await self.close()

    async def close(self) -> None:
        self._closed = True
        if self.process is not None:
            await kill_group(self.process)

    @property
    def reaped(self) -> bool:
        return self.process is not None and self.process.returncode is not None


class CliBackend:
    """Everything goes through ``agentbox ... --json``."""

    def __init__(self, cli: str = DEFAULT_CLI, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.cli = cli
        self.timeout_s = timeout_s

    async def _run(self, *args: str) -> str:
        """Run one CLI call to completion. A timeout or a cancellation kills the child."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.cli,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise BackendError(f"cannot run {self.cli}: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout_s)
        except TimeoutError:
            await kill_group(process)
            raise BackendError(f"{args[0]} timed out after {self.timeout_s:g}s") from None
        finally:
            if process.returncode is None:  # cancelled mid-call: never abandon the child
                await kill_group(process)
        if process.returncode != 0:
            reason = _one_line(stderr.decode("utf-8", "replace")) or "no output"
            raise BackendError(f"{args[0]} exited {process.returncode}: {reason}")
        return stdout.decode("utf-8", "replace")

    async def _run_json(self, *args: str) -> Any:
        text = await self._run(*args)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise BackendError(f"{args[0]}: not JSON ({exc.msg})") from exc

    async def status(self) -> Status:
        data = await self._run_json("status", "--json")
        if not isinstance(data, dict):
            raise BackendError("status: expected an object")
        return Status.from_json(data)

    async def runs(self, target: str) -> list[dict[str, Any]]:
        data = await self._run_json("runs", "--json", "--", check_operand(target, "target"))
        if not isinstance(data, list):
            raise BackendError("runs: expected an array")
        return [r for r in data if isinstance(r, dict)]

    def follow(self, target: str, runid: str | None) -> ProcessLogFollower:
        argv = [self.cli, "logs", "-f", "--json", "--", check_operand(target, "target")]
        if runid:
            argv.append(check_operand(runid, "run id"))
        return ProcessLogFollower(argv)

    async def stop_run(self, target: str, runid: str | None) -> None:
        args = ["stop-run", "--", check_operand(target, "target")]
        if runid:
            args.append(check_operand(runid, "run id"))
        await self._run(*args)

    def attach_argv(self, target: str, session: str | None) -> list[str]:
        argv = [self.cli, "attach", "--", check_operand(target, "target")]
        if session:
            argv.append(check_operand(session, "session"))
        return argv


# ---------------------------------------------------------------------- fixtures


class FixtureLogFollower:
    """Replays ``logs.jsonl`` with a small gap between lines, or nothing at all."""

    def __init__(self, path: Path | None, gap_s: float = 0.05) -> None:
        self.path = path
        self.gap_s = gap_s
        self._closed = False

    async def start(self) -> None:
        return None

    async def __aiter__(self) -> AsyncIterator[Event]:
        if self.path is None:
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise BackendError(f"cannot read {self.path.name}: {exc}") from exc
        for line in lines:
            if self._closed:
                return
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                yield Event.from_json(data)
            await asyncio.sleep(self.gap_s)

    async def close(self) -> None:
        self._closed = True


class FixtureBackend:
    """Reads ``status.json``, ``runs.json`` and ``logs.jsonl`` from a directory.

    ``runs`` and ``logs`` are keyed by target the way the CLI would answer: a box
    whose fixture ``run`` is null and ``runs_total`` is 0 has no runs and no log.
    """

    def __init__(self, directory: Path, gap_s: float = 0.05) -> None:
        self.directory = Path(directory)
        self.gap_s = gap_s
        self.stop_calls: list[tuple[str, str | None]] = []

    def _read_json(self, name: str) -> Any:
        path = self.directory / name
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise BackendError(f"cannot read {name}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise BackendError(f"{name}: not JSON ({exc.msg})") from exc

    def _box(self, target: str) -> Box | None:
        data = self._read_json("status.json")
        if not isinstance(data, dict):
            return None
        for box in Status.from_json(data).boxes:
            if box.target == target or box.name == target:
                return box
        return None

    def _has_runs(self, target: str) -> bool:
        box = self._box(check_operand(target, "target"))
        return box is not None and (box.run is not None or box.runs_total > 0)

    async def status(self) -> Status:
        data = self._read_json("status.json")
        if not isinstance(data, dict):
            raise BackendError("status.json: expected an object")
        return Status.from_json(data)

    async def runs(self, target: str) -> list[dict[str, Any]]:
        if not self._has_runs(target):
            return []
        data = self._read_json("runs.json")
        if not isinstance(data, list):
            raise BackendError("runs.json: expected an array")
        return [r for r in data if isinstance(r, dict)]

    def follow(self, target: str, runid: str | None) -> FixtureLogFollower:
        if runid:
            check_operand(runid, "run id")
        path = self.directory / "logs.jsonl" if self._has_runs(target) else None
        return FixtureLogFollower(path, gap_s=self.gap_s)

    async def stop_run(self, target: str, runid: str | None) -> None:
        check_operand(target, "target")
        self.stop_calls.append((target, runid))

    def attach_argv(self, target: str, session: str | None) -> None:
        return None


def resolve_cli(explicit: str | None) -> str:
    """``--agentbox`` wins, then ``$AGENTBOX``, then ``agentbox`` on PATH."""
    if explicit:
        return explicit
    env = os.environ.get("AGENTBOX")
    if env:
        return env
    return shutil.which(DEFAULT_CLI) or DEFAULT_CLI
