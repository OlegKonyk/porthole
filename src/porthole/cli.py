"""``porthole [--agentbox PATH] [--interval SECS] [--fixtures DIR]``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .app import PortholeApp
from .backend import Backend, CliBackend, FixtureBackend, resolve_cli


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="porthole",
        description="A terminal window into every agent box. Renders agentbox --json output.",
    )
    parser.add_argument(
        "--agentbox",
        metavar="PATH",
        help="the agentbox CLI to call (default: $AGENTBOX, then agentbox on PATH)",
    )
    parser.add_argument(
        "--interval",
        metavar="SECS",
        type=float,
        default=3.0,
        help="status poll interval in seconds (default: 3)",
    )
    parser.add_argument(
        "--fixtures",
        metavar="DIR",
        type=Path,
        help="replay status.json, runs.json and logs.jsonl from DIR instead of calling the CLI",
    )
    parser.add_argument("--version", action="version", version=f"porthole {__version__}")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    return args


def build_backend(args: argparse.Namespace) -> Backend:
    if args.fixtures is not None:
        return FixtureBackend(args.fixtures)
    # A poll that outlives its interval becomes a header error, never a pile of children.
    return CliBackend(resolve_cli(args.agentbox), timeout_s=max(10.0, 3.0 * args.interval))


def build_app(argv: Sequence[str] | None = None) -> PortholeApp:
    args = parse_args(argv)
    return PortholeApp(build_backend(args), interval=args.interval)


def main(argv: Sequence[str] | None = None) -> int:
    app = build_app(argv)
    app.run()
    return app.return_code or 0
