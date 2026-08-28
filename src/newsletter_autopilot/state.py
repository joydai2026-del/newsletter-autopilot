"""Crash-safe run state.

The irreversible step (scheduling a post that emails a real subscriber list)
sits in the middle of a multi-step sequence. A crash, a second concurrent run,
or a timeout that hides a server-side success must never produce two issues.

The contract:

  * One state file per ISSUE DATE, not per invocation. Two runs for the same
    issue share state; runs for different issues never collide.
  * Every transition is written atomically BEFORE the external call it
    describes, and confirmed after. A crash therefore leaves a state that is
    pessimistic (it may claim an action was attempted that never landed) and
    never optimistic.
  * PARKED is terminal and requires a human. Nothing here auto-retries an
    external write. An ambiguous outcome is not a retry signal, it is a stop
    signal, because the ambiguous case is exactly the one where the server may
    already have done the thing.

Phases, in order:

    selected        sources chosen and RECORDED (nothing remote yet)
    drafted         a remote draft exists, id recorded
    schedule_pending  the schedule call is about to fire / fired, outcome unknown
    scheduled       the platform confirmed the schedule, verified by read-back
    sources_marked  the source rows are checked off
    complete        done
    parked          needs a human; never resumed automatically

Standard library only, on purpose: this is the component that must not fail, so
it takes no dependency that could.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .errors import Parked, StateError

PHASES = (
    "selected",
    "drafted",
    "schedule_pending",
    "scheduled",
    "sources_marked",
    "complete",
)
TERMINAL = ("complete", "parked")

# The one park a recovery is allowed to pick up, because it happens before any
# client is constructed and so provably changed nothing remote.
CUTOFF_PARK_REASON = "reached publishing after the cutoff; nothing was contacted"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON so a crash mid-write can never leave a truncated state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass
class RunState:
    """The durable record of one issue's journey to the platform."""

    issue_date: str                                  # YYYY-MM-DD
    state_dir: Path = Path(".autopilot/state")
    phase: str = "selected"
    source_ids: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    draft_id: str | None = None
    created_title: str | None = None                 # proves a resumed draft is ours
    scheduled_trigger_at: str | None = None
    park_reason: str | None = None
    pre_park_phase: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)   # append-only trail

    # ---- persistence -----------------------------------------------------

    @property
    def path(self) -> Path:
        return Path(self.state_dir) / f"run-{self.issue_date}.json"

    @classmethod
    def load(cls, issue_date: str, state_dir: Path | str) -> RunState | None:
        p = Path(state_dir) / f"run-{issue_date}.json"
        if not p.exists():
            return None
        with open(p, encoding="utf8") as fh:
            raw = json.load(fh)
        # Ignore fields this version does not know rather than raising. A state
        # file written by a newer copy used to blow up with a bare TypeError
        # inside the handlers that exist precisely to make sure a human gets
        # told something, so an unknown key meant a traceback and no alert.
        known = {f.name for f in fields(cls)} - {"state_dir"}
        obj = cls(issue_date=issue_date, state_dir=Path(state_dir))
        for k, v in raw.items():
            if k in known:
                setattr(obj, k, v)
        return obj

    @classmethod
    def load_or_create(cls, issue_date: str, state_dir: Path | str) -> RunState:
        return cls.load(issue_date, state_dir) or cls(issue_date=issue_date,
                                                      state_dir=Path(state_dir))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("state_dir", None)
        return data

    def save(self) -> None:
        atomic_write_json(self.path, self.to_dict())

    # ---- transitions -----------------------------------------------------

    def _disk_events(self) -> list[dict[str, Any]] | None:
        try:
            with open(self.path, encoding="utf8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        events = data.get("events") if isinstance(data, dict) else None
        return events if isinstance(events, list) else None

    def record(self, event: str, **detail: Any) -> None:
        """Append to the audit trail and persist.

        The trail is APPEND-ONLY ACROSS PROCESSES, not just within one. Commands
        that load a snapshot, do slow remote work, and only then write would
        otherwise erase whatever another run wrote meanwhile, including the
        at-most-once recovery marker whose disappearance is what permits a
        second issue to be sent. So re-read the trail immediately before
        appending and keep anything that arrived in the meantime.
        """
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **detail}
        disk = self._disk_events()
        if disk is not None and len(disk) > len(self.events):
            merged = list(disk)
            merged.extend(own for own in self.events if own not in merged)
            self.events = merged
        self.events.append(entry)
        self.save()

    def advance(self, phase: str, **detail: Any) -> None:
        """Move to `phase`. Refuses to move backwards, because a backwards move
        is always a bug and would re-open an irreversible step."""
        if self.phase == "parked":
            raise Parked(f"run {self.issue_date} is parked: {self.park_reason}")
        if phase not in PHASES:
            raise StateError(f"unknown phase {phase!r}")
        if self.phase in PHASES and PHASES.index(phase) < PHASES.index(self.phase):
            raise StateError(f"refusing to move backwards: {self.phase} -> {phase}")
        self.phase = phase
        self.record(f"phase:{phase}", **detail)

    def park(self, reason: str, **detail: Any) -> None:
        """Terminal stop. The ONLY correct response to an ambiguous outcome."""
        if self.phase != "parked":
            self.pre_park_phase = self.phase
        self.phase = "parked"
        self.park_reason = reason
        self.record("parked", reason=reason, **detail)

    def unpark(self, reason: str) -> None:
        """Resume a park that provably changed NOTHING remote.

        Parked is terminal on purpose and stays terminal for every park that
        followed a remote call. This exists for the cutoff park alone, which
        happens before a client is even constructed: a run that stopped there is
        indistinguishable from a run that never started, so the one supported
        same-day recovery has to be able to pick it up. Without this, the
        advertised recovery path refuses the exact state it was built for.
        """
        if self.park_reason != CUTOFF_PARK_REASON:
            raise Parked(
                f"refusing to unpark {self.issue_date}: this park followed a remote "
                f"call ({self.park_reason}), so what is on the platform is not proven")
        self.phase = self.pre_park_phase or "selected"
        self.park_reason = None
        self.pre_park_phase = None
        self.record("unparked", reason=reason, phase=self.phase)

    def reached(self, phase: str) -> bool:
        """True if the run is at or past `phase`. Makes each step a no-op on a
        resumed run rather than repeating an external write."""
        if self.phase == "parked":
            return False
        if self.phase == "complete":
            return True
        return self.phase in PHASES and PHASES.index(self.phase) >= PHASES.index(phase)

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL

    # ---- at-most-once recovery bookkeeping -------------------------------

    def recovery_attempts(self) -> int:
        return len([e for e in self.events if e.get("event") == "recovery_attempted"])

    def recovery_released(self) -> dict[str, Any] | None:
        """The recorded proof that a recovery already released this issue.

        Its ABSENCE after an attempt is what tells a later run "something may
        have landed"; its presence is what makes a second release refuse.
        """
        done = [e for e in self.events
                if e.get("event") == "recovery_outcome" and e.get("status") == "scheduled"]
        return done[-1] if done else None
