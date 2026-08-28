"""Run policy, as data.

Every operational value that could reasonably change lives here and is loaded
from a TOML file or overridden per call. None of it is a constant buried in the
pipeline, because changing "we send at 09:00" or "three recovery attempts is
the limit" must never require editing the code that emails people.

The defaults below are a working policy, not a placeholder: `autopilot dry-run`
uses exactly these.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

try:                                    # 3.11+
    import tomllib
except ModuleNotFoundError:             # pragma: no cover
    tomllib = None                      # type: ignore[assignment]

from .errors import ConfigError

# Weekday numbers as datetime uses them: Monday is 0.
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@dataclass(frozen=True)
class Policy:
    """Everything about WHEN and HOW MUCH. Swappable without touching a stage."""

    # --- schedule ---------------------------------------------------------
    timezone: str = "America/New_York"
    publish_at: str = "09:00"                     # local time on the issue's own day
    publish_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    # No remote write inside this margin before the slot. Past it the run parks
    # rather than racing its own send.
    cutoff_minutes: int = 10

    # --- recovery ---------------------------------------------------------
    # A same-day recovery still RELEASES BY SCHEDULING, a few minutes out. It
    # never calls an immediate-publish endpoint.
    recovery_lead_minutes: int = 5
    max_recovery_attempts: int = 3

    # --- locking ----------------------------------------------------------
    # A lock older than this whose holder cannot be identified is treated as
    # abandoned. An identifiable live holder is NEVER reclaimed on age alone.
    stale_lock_seconds: int = 30 * 60

    # --- content ----------------------------------------------------------
    stories_per_issue: int = 5
    images_per_issue: int = 5
    forbid_em_dashes: bool = True

    # --- watchdog ---------------------------------------------------------
    # The watchdog asks the archive after the send slot has had time to land.
    watchdog_grace_minutes: int = 60

    @property
    def tz(self) -> zoneinfo.ZoneInfo:
        try:
            return zoneinfo.ZoneInfo(self.timezone)
        except Exception as exc:                       # noqa: BLE001
            raise ConfigError(f"unknown timezone {self.timezone!r}: {exc}") from None

    def slot_on(self, date: str, at: str | None = None) -> dt.datetime:
        """The aware local datetime of the send slot on `date`."""
        at = at or self.publish_at
        try:
            hh, mm = (int(part) for part in at.split(":", 1))
            day = dt.date.fromisoformat(date)
        except ValueError as exc:
            raise ConfigError(f"bad date {date!r} or time {at!r}: {exc}") from None
        return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=self.tz)

    def is_publishing_day(self, date: str) -> bool:
        return dt.date.fromisoformat(date).weekday() in self.publish_days


@dataclass(frozen=True)
class Config:
    """Policy plus the paths and identifiers a run needs."""

    policy: Policy = field(default_factory=Policy)
    # Where durable run state, locks, and ledgers live. One directory, so a
    # deployment can point the whole system somewhere else in one line.
    home: Path = Path(".autopilot")
    publication_name: str = "Example Daily"
    # The public archive feed the watchdog reads. Deliberately the PUBLIC one:
    # the watchdog must be able to answer "did readers get it" without any
    # credential the pipeline holds.
    archive_url: str = ""

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def ledger_dir(self) -> Path:
        return self.home / "ledgers"

    @property
    def output_dir(self) -> Path:
        return self.home / "out"

    def with_home(self, home: Path | str) -> Config:
        return replace(self, home=Path(home))

    # --- loading ----------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str | None) -> Config:
        """Read a TOML policy file. No file means the documented defaults."""
        if path is None:
            return cls()
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"no config file at {p}")
        if tomllib is None:                              # pragma: no cover
            raise ConfigError("tomllib is unavailable; Python 3.11+ is required")
        raw: dict[str, Any] = tomllib.loads(p.read_text(encoding="utf8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        pol_raw = dict(raw.get("policy") or {})
        days = pol_raw.get("publish_days")
        if days is not None:
            pol_raw["publish_days"] = tuple(_weekday(d) for d in days)
        known = {f.name for f in Policy.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        unknown = set(pol_raw) - known
        if unknown:
            raise ConfigError(f"unknown policy keys: {', '.join(sorted(unknown))}")
        policy = Policy(**pol_raw)

        top = {k: v for k, v in raw.items() if k != "policy"}
        cfg_known = {f.name for f in cls.__dataclass_fields__.values()  # type: ignore[attr-defined]
                     if f.name != "policy"}
        unknown = set(top) - cfg_known
        if unknown:
            raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")
        if "home" in top:
            top["home"] = Path(top["home"]).expanduser()
        return cls(policy=policy, **top)


def _weekday(value: Any) -> int:
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ConfigError(f"weekday {value} is out of range 0-6")
    key = str(value).strip().lower()[:3]
    if key not in _WEEKDAYS:
        raise ConfigError(f"unknown weekday {value!r}")
    return _WEEKDAYS[key]
