"""Exclusive per-issue lock, and the asymmetry that makes it safe.

THE ASYMMETRY (the point of this module)

Two mistakes are possible when a lock file is found:

    steal a lock whose holder is ALIVE  -> two runs proceed, neither reconcile
                                           sees the other's not-yet-scheduled
                                           draft, and TWO issues are emailed.
                                           IRRECOVERABLE.
    hold a lock whose holder is DEAD    -> today's issue does not go out, and
                                           the operator is notified. Recoverable
                                           in minutes.

The costs are not symmetric, so the logic is not symmetric either. Every
uncertain answer resolves toward "the holder is alive": an unreadable lock, a
`ps` that errors, an OSError from the liveness probe. Only a positive proof of
death (the pid does not exist) or an unidentifiable lock that is also long stale
permits a reclaim.

WHY AGE IS NOT PROOF

The obvious implementation reclaims any lock older than N minutes. That is
exactly the irrecoverable mistake: a slow-but-alive run gets its lock stolen at
minute N+1. Age is used here only as a last resort, for a lock that names no pid
we can check.

WHY A PID IS NOT AN IDENTITY

Pids get recycled. A recycled pid makes an abandoned lock look alive forever,
which silently kills the newsletter with no notification. pid PLUS process start
time is a real identity: the OS will not hand out the same pair twice.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .errors import LockHeld


def _read_token(path: Path) -> str | None:
    """A lock file's token, or None if it cannot be read."""
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return None


def _holder_pid(holder: str) -> int | None:
    """The pid recorded in a lock token, or None if it names none we can parse."""
    for part in holder.split():
        if part.startswith("pid="):
            try:
                return int(part[4:])
            except ValueError:
                return None
    return None


def _proc_start(pid: int) -> str | None:
    """The process start time as the OS reports it, or None if we cannot tell.

    Returns None whenever it cannot get a clean answer, and NOTHING here ever
    counts as proof of death. That is the whole safety property. Death is
    established earlier and independently by os.kill(pid, 0); by the time this
    runs the pid is known to exist, so a nonzero exit means the probe FAILED,
    not that the process is gone. Reading a probe failure as death would steal a
    live lock and email two issues.
    """
    try:
        proc = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                              capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None                       # cannot ask
    if proc.returncode != 0:
        return None                       # the probe errored. Not a death certificate
    return proc.stdout.strip() or None    # success with no output is not an answer either


def holder_alive(holder: str) -> bool:
    """True unless the exact process that wrote this lock is provably gone."""
    pid = _holder_pid(holder)
    if pid is None or pid <= 0:
        return False                      # unidentifiable; age decides, in __enter__
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False                      # positive proof of death
    except PermissionError:
        pass                              # exists, owned by someone else
    except OSError:
        return True                       # unknown: never reclaim on a guess

    # `start=` is written LAST in the token, so everything after it is the
    # timestamp, which itself contains spaces.
    _, sep, recorded = holder.partition("start=")
    if not sep or not recorded.strip():
        return True                       # old-format lock: assume alive, age decides

    current = _proc_start(pid)
    if current is None:
        return True                       # could not ask the OS. Uncertainty is ALIVE
    return current == recorded.strip()


class RunLock:
    """Exclusive per-issue-date lock, as a context manager.

    Uses O_CREAT|O_EXCL, which is atomic on every POSIX filesystem worth
    deploying on, rather than a check-then-create that two runs could both pass.
    """

    def __init__(self, issue_date: str, state_dir: Path | str,
                 stale_seconds: int = 30 * 60):
        self.path = Path(state_dir) / f"run-{issue_date}.lock"
        self.stale_seconds = stale_seconds
        self._fd: int | None = None
        self._token: str | None = None

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._open()
        except FileExistsError:
            try:
                holder = self.path.read_text(errors="replace").strip()
                age = time.time() - self.path.stat().st_mtime
            except OSError as exc:
                # Cannot even read the lock. That is uncertainty, and uncertainty
                # means the holder is alive.
                raise LockHeld(f"a lock exists at {self.path.name} and could not be "
                               f"read ({exc}); treating the holder as alive") from None

            if holder_alive(holder):
                raise LockHeld(
                    f"another run holds {self.path.name} and its process is still "
                    f"alive (age {int(age)}s, holder {holder})") from None
            if _holder_pid(holder) is None and age < self.stale_seconds:
                raise LockHeld(
                    f"another run holds {self.path.name}, which names no checkable "
                    f"process (age {int(age)}s, holder {holder})") from None

            # Provably gone, or unidentifiable AND long stale: reclaim.
            #
            # NOT unlink-then-create. That is the very check-then-act this class
            # exists to avoid: two runs can both see the same dead holder, and
            # the second one's unlink would delete the FIRST one's brand new,
            # live lock, after which both proceed and two issues go out.
            #
            # Instead move the dead lock aside with a rename, which is atomic,
            # then PROVE what was moved was the dead holder we inspected. If it
            # was not, someone else reclaimed in the gap and what we just moved
            # is their live lock: put it straight back and yield.
            stale = self.path.with_name(f"{self.path.name}.stale.{os.getpid()}")
            try:
                os.rename(self.path, stale)
            except (FileNotFoundError, OSError) as exc:
                raise LockHeld(
                    f"{self.path.name} changed while this run was reclaiming it "
                    f"({exc}); yielding rather than racing") from None

            moved = _read_token(stale)
            if moved != holder:
                try:
                    os.rename(stale, self.path)       # put the live lock back
                except OSError:
                    pass
                raise LockHeld(
                    f"another run reclaimed {self.path.name} first; its lock was "
                    "restored and this run is yielding") from None
            try:
                self._open()
            except FileExistsError:
                # A third run created a fresh lock in the gap. Contention, not a
                # disk fault, and it has to say so: a bare FileExistsError is an
                # OSError, and the caller's OSError handler reports disk trouble
                # and tells the operator to go check their files. Right
                # behaviour, wrong explanation, in the one module whose whole job
                # is an honest account of what happened.
                raise LockHeld(
                    f"another run took {self.path.name} while this one was "
                    "reclaiming it from a dead holder") from None
            finally:
                try:
                    os.unlink(stale)
                except OSError:
                    pass
        return self

    def _open(self) -> None:
        me = os.getpid()
        started = _proc_start(me)
        # Omit start= entirely if it could not be read, rather than writing an
        # empty one. A token with no start time falls back to pid-plus-age, which
        # errs toward HOLDING the lock, and a held lock notifies. An EMPTY start=
        # would instead never match a real one, and the lock would be stolen.
        self._token = (f"pid={me} at={time.strftime('%Y-%m-%dT%H:%M:%S%z')}"
                       + (f" start={started}" if started else ""))
        self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(self._fd, (self._token + "\n").encode())
        os.fsync(self._fd)

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        # Only remove the lock if it is still OURS. If this run was wrongly
        # declared stale and another run took the lock, deleting it here would
        # hand a third run a free pass while the second is still working.
        try:
            if self.path.read_text(errors="replace").strip() == self._token:
                self.path.unlink(missing_ok=True)
        except OSError:
            pass
