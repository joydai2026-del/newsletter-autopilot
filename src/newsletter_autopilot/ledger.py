"""Append-only dedup ledgers: what has already been used, and when.

WHY THIS IS CODE AND NOT A PROMPT STEP

The original pipeline's checklist had always said "record what this issue used"
as its last step, and for four consecutive issues nobody did. A step that exists
only as an instruction to a model or a human is a step that silently stops
happening, and the failure is invisible until something repeats in front of the
whole list. So it moved next to the other things that must happen every time,
and is called by the pipeline after the schedule lands.

TWO ORDERING RULES, both load-bearing:

  * Record AFTER the post is scheduled, never before. A ledger entry for an
    issue that never went out causes the opposite defect: assets retired without
    ever having been seen.
  * A ledger failure never stops or re-runs anything. By the time it runs the
    post is already scheduled, and the only cost of a miss is a repeat.

The files are append-only markdown a human reads directly, so the shape is kept
deliberately plain:

    <kind>.md     ## YYYY-MM-DD    followed by one "- <entry>" per item

Idempotent by date: re-running for a date already recorded changes nothing, so a
resumed or re-run publish cannot double-write.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import LedgerError


class Ledger:
    """One append-only, idempotent-by-date record of used assets."""

    def __init__(self, kind: str, ledger_dir: Path | str):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", kind):
            raise LedgerError(f"unsafe ledger name {kind!r}")
        self.kind = kind
        self.path = Path(ledger_dir) / f"{kind}.md"

    # --- reads ------------------------------------------------------------

    def _read(self) -> str:
        return self.path.read_text(encoding="utf8") if self.path.exists() else ""

    def recorded(self, date: str) -> bool:
        return re.search(rf"^## {re.escape(date)}\s*$", self._read(), re.M) is not None

    def all_entries(self) -> set[str]:
        return set(re.findall(r"^- (.+)$", self._read(), re.M))

    def entries_for(self, date: str) -> list[str]:
        block = re.search(rf"^## {re.escape(date)}\s*$\n((?:- .*\n?)*)",
                          self._read(), re.M)
        if not block:
            return []
        return [line[2:].strip() for line in block.group(1).splitlines()
                if line.startswith("- ")]

    def seen(self, entry: str) -> bool:
        """Has this asset been used in any past issue?"""
        return entry.strip() in self.all_entries()

    # --- writes -----------------------------------------------------------

    def record(self, date: str, entries: list[str]) -> bool:
        """Record this issue's entries. False if the date was already present.

        Refuses an empty list: an empty record would set the idempotency guard
        for that date and make the real entries unrecordable forever.
        """
        clean = [e.strip() for e in entries if e and e.strip()]
        if not clean:
            raise LedgerError(f"refusing to record an empty {self.kind} list for {date}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise LedgerError(f"bad date {date!r}")
        if self.recorded(date):
            return False
        self._append(f"## {date}\n" + "\n".join(f"- {e}" for e in clean))
        return True

    def _append(self, block: str) -> None:
        """Atomic whole-file replace, not a plain append.

        The reason is specific: `recorded()` answers "already done?" by looking
        for the `## <date>` heading. A torn append that wrote the heading and
        then died would make that return True with zero or partial entries, and
        the idempotency guard would then skip that date forever. Whole-file
        replace means a date is either fully present or fully absent.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self._read()
        if not existing:
            sep = ""
        elif existing.endswith("\n\n"):
            sep = ""
        elif existing.endswith("\n"):
            sep = "\n"
        else:
            sep = "\n\n"
        payload = existing + sep + block.rstrip("\n") + "\n"

        tmp = self.path.with_suffix(self.path.suffix + f".tmp{os.getpid()}")
        with open(tmp, "w", encoding="utf8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)


def record_issue(ledger_dir: Path | str, date: str,
                 assets: dict[str, list[str]]) -> dict[str, bool]:
    """Record every ledger for one issue.

    EVERY write is ATTEMPTED even if an earlier one fails, so a disk error on one
    ledger cannot also cost the others. They are independent; there is no
    ordering worth preserving, only a reason not to let one failure become
    several. If any failed, the errors are raised together at the end so the
    caller records the truth rather than a partial success.
    """
    out: dict[str, bool] = {}
    errors: list[str] = []
    for kind, entries in assets.items():
        try:
            out[kind] = Ledger(kind, ledger_dir).record(date, entries)
        except (LedgerError, OSError) as exc:
            out[kind] = False
            errors.append(f"{kind}: {exc}")
    if errors:
        raise LedgerError("; ".join(errors))
    return out


def filter_unused(ledger_dir: Path | str, kind: str, candidates: list[str]) -> list[str]:
    """Candidates that no past issue has used. Order preserved."""
    led = Ledger(kind, ledger_dir)
    used = led.all_entries()
    out, seen_now = [], set()
    for c in candidates:
        key = c.strip()
        if key and key not in used and key not in seen_now:
            seen_now.add(key)
            out.append(c)
    return out
