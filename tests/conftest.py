from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from newsletter_autopilot.adapters.mock import MockPublisher  # noqa: E402
from newsletter_autopilot.config import Config, Policy  # noqa: E402
from newsletter_autopilot.notify import ConsoleNotifier  # noqa: E402
from newsletter_autopilot.pipeline import Pipeline  # noqa: E402

ISSUE_DATE = "2026-08-26"          # a Wednesday


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(policy=Policy(), home=tmp_path / "home",
                  publication_name="Test Daily")


@pytest.fixture
def stories(tmp_path: Path) -> Path:
    src = json.loads((ROOT / "examples" / "sample-stories.json").read_text("utf8"))
    p = tmp_path / "stories.json"
    p.write_text(json.dumps(src), encoding="utf8")
    return p


@pytest.fixture
def adapter(cfg: Config) -> MockPublisher:
    return MockPublisher(cfg.home / "platform", publication="Test Daily")


def at(when: str, cfg: Config):
    """A frozen clock at a local time on the issue date."""
    moment = dt.datetime.fromisoformat(f"{ISSUE_DATE}T{when}").replace(tzinfo=cfg.policy.tz)
    return lambda: moment


@pytest.fixture
def pipe(adapter: MockPublisher, cfg: Config) -> Pipeline:
    # Well before the 09:00 slot, so the cutoff is not in play by default.
    return Pipeline(adapter, cfg, ConsoleNotifier(), clock=at("06:00", cfg))


def make_pipe(adapter, cfg, when: str = "06:00") -> Pipeline:
    return Pipeline(adapter, cfg, ConsoleNotifier(), clock=at(when, cfg))
