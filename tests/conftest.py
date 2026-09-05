from __future__ import annotations

from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
FAKE = TESTS / "fake-agentbox"
FIXTURES = TESTS / "fixtures"


@pytest.fixture
def fake_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point AGENTBOX at the fake and give it a call log to append to."""
    log = tmp_path / "calls.log"
    monkeypatch.setenv("AGENTBOX", str(FAKE))
    monkeypatch.setenv("FAKE_AGENTBOX_LOG", str(log))
    monkeypatch.delenv("FAKE_AGENTBOX_FAIL_STATUS", raising=False)
    return log


def calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [line.split() for line in log.read_text().splitlines() if line.strip()]
