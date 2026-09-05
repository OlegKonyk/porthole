from __future__ import annotations

import pytest

from porthole.backend import CliBackend, FixtureBackend, resolve_cli
from porthole.cli import build_backend, parse_args

from .conftest import FIXTURES


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTBOX", raising=False)
    args = parse_args([])
    assert args.interval == 3.0
    assert args.fixtures is None
    backend = build_backend(args)
    assert isinstance(backend, CliBackend)
    assert backend.cli.endswith("agentbox")


def test_agentbox_flag_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBOX", "/from/env")
    assert resolve_cli(None) == "/from/env"
    assert resolve_cli("/from/flag") == "/from/flag"
    backend = build_backend(parse_args(["--agentbox", "/from/flag"]))
    assert isinstance(backend, CliBackend) and backend.cli == "/from/flag"


def test_fixtures_flag() -> None:
    backend = build_backend(parse_args(["--fixtures", str(FIXTURES), "--interval", "0.5"]))
    assert isinstance(backend, FixtureBackend)
    assert backend.directory == FIXTURES


def test_interval_must_be_positive() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--interval", "0"])
