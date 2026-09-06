"""The agentbox --json contract, as plain data, plus the formatting the table needs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

RUN_STATES = ("running", "done", "failed", "stopped", "lost", "unknown")
# `lost`: the run's process vanished without recording an exit (the CLI reconciles it).
# `unknown`: a run directory with no status file. Neither counts as running.
# The box's egress mode. `drop` was the old name for `deny` and is mapped to it.
EGRESS_MODES = ("deny", "observe", "open", "unknown")
EVENT_KINDS = ("text", "tool", "tool_result", "hook", "result", "status")


@dataclass(frozen=True)
class Run:
    id: str
    state: str
    exit: int | None = None
    model: str | None = None
    branch: str | None = None
    started_at: str | None = None
    elapsed_s: float | None = None
    turns: int | None = None
    cost_usd: float | None = None
    last_tool: str | None = None
    last_text: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Run:
        return cls(
            id=str(data.get("id", "")),
            state=str(data.get("state", "")),
            exit=data.get("exit"),
            model=data.get("model"),
            branch=data.get("branch"),
            started_at=data.get("started_at"),
            elapsed_s=data.get("elapsed_s"),
            turns=data.get("turns"),
            cost_usd=data.get("cost_usd"),
            last_tool=data.get("last_tool"),
            last_text=data.get("last_text"),
        )


@dataclass(frozen=True)
class Session:
    name: str
    age_s: float | None = None


@dataclass(frozen=True)
class Box:
    name: str
    instance: str
    repo: str | None
    state: str
    claude_version: str | None = None
    firewall: str = "unknown"
    run: Run | None = None
    runs_total: int = 0
    sessions: tuple[Session, ...] = ()

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Box:
        run = data.get("run")
        return cls(
            name=str(data.get("name", "")),
            instance=str(data.get("instance", "")),
            repo=data.get("repo"),
            state=str(data.get("state", "stopped")),
            claude_version=data.get("claude_version"),
            firewall=egress_mode(data.get("firewall")),
            run=Run.from_json(run) if isinstance(run, dict) else None,
            runs_total=int(data.get("runs_total") or 0),
            sessions=tuple(
                Session(name=str(s.get("name", "")), age_s=s.get("age_s"))
                for s in data.get("sessions") or []
            ),
        )

    @property
    def target(self) -> str:
        """What to pass to the CLI as ``<repo>``: the repo path, or the box name without one."""
        return self.repo or self.name

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    @property
    def has_running_run(self) -> bool:
        return self.run is not None and self.run.state == "running"

    @property
    def sort_key(self) -> tuple[int, str]:
        if self.has_running_run:
            rank = 0
        elif self.is_running:
            rank = 1
        else:
            rank = 2
        return (rank, self.name)


@dataclass(frozen=True)
class Status:
    generated_at: str | None
    boxes: tuple[Box, ...] = field(default_factory=tuple)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Status:
        boxes = tuple(Box.from_json(b) for b in data.get("boxes") or [])
        return cls(generated_at=data.get("generated_at"), boxes=boxes)

    @property
    def sorted_boxes(self) -> list[Box]:
        return sorted(self.boxes, key=lambda b: b.sort_key)

    @property
    def running_runs(self) -> int:
        return sum(1 for b in self.boxes if b.has_running_run)

    def age_s(self, now: datetime | None = None) -> float | None:
        stamp = parse_iso(self.generated_at)
        if stamp is None:
            return None
        now = now or datetime.now(UTC)
        return max(0.0, (now - stamp).total_seconds())


@dataclass(frozen=True)
class Event:
    ts: str | None
    run: str | None
    kind: str
    text: str
    tool: str | None = None
    detail: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Event:
        kind = str(data.get("kind") or "text")
        if kind not in EVENT_KINDS:
            kind = "text"
        return cls(
            ts=data.get("ts"),
            run=data.get("run"),
            kind=kind,
            text=str(data.get("text") or ""),
            tool=data.get("tool"),
            detail=data.get("detail"),
        )


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp


def fmt_elapsed(seconds: float | None) -> str:
    """mm:ss; minutes keep growing past 59 rather than rolling into hours."""
    if seconds is None:
        return ""
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    return f"{seconds / 3600:.1f}h ago"


def fmt_cost(cost: float | None) -> str:
    return "" if cost is None else f"${cost:.2f}"


def fmt_turns(turns: int | None) -> str:
    return "" if turns is None else str(turns)


def egress_mode(value: Any) -> str:
    """Normalise the status contract's ``firewall`` field to one of EGRESS_MODES."""
    mode = str(value or "unknown")
    if mode == "drop":
        return "deny"
    return mode


def egress_style(mode: str) -> str:
    """deny and unknown dimmed, observe yellow, open red: the louder the more it lets out."""
    if mode == "observe":
        return "yellow"
    if mode == "open":
        return "red"
    return "dim"


def run_state_style(state: str) -> str:
    """failed and lost share a colour; unknown is dimmed; the rest use the theme's text."""
    if state in ("failed", "lost"):
        return "red"
    if state == "unknown":
        return "dim"
    return ""


def fmt_ts(ts: str | None) -> str:
    stamp = parse_iso(ts)
    return stamp.strftime("%H:%M:%S") if stamp else "        "
