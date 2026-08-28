"""The lock asymmetry: stealing a live lock is irrecoverable, holding a dead one
is a notification. Every uncertain answer must resolve toward "alive"."""

from __future__ import annotations

import os
import time

import pytest

from newsletter_autopilot import locking
from newsletter_autopilot.errors import LockHeld
from newsletter_autopilot.locking import RunLock, holder_alive


def test_lock_is_exclusive(tmp_path):
    with RunLock("2026-08-26", tmp_path):
        with pytest.raises(LockHeld):
            with RunLock("2026-08-26", tmp_path):
                pass


def test_different_dates_do_not_collide(tmp_path):
    with RunLock("2026-08-26", tmp_path), RunLock("2026-08-27", tmp_path):
        pass


def test_lock_is_released_on_exit(tmp_path):
    lock = RunLock("2026-08-26", tmp_path)
    with lock:
        assert lock.path.exists()
    assert not lock.path.exists()


def test_a_live_holder_is_never_reclaimed_on_age(tmp_path, monkeypatch):
    """The irrecoverable mistake. An ancient lock whose process is alive must
    still be held."""
    path = tmp_path / "run-2026-08-26.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"pid={os.getpid()} at=x start={locking._proc_start(os.getpid())}\n")
    old = time.time() - 60 * 60 * 24
    os.utime(path, (old, old))

    with pytest.raises(LockHeld) as exc:
        with RunLock("2026-08-26", tmp_path, stale_seconds=1):
            pass
    assert "still alive" in str(exc.value)


def test_a_provably_dead_holder_is_reclaimed(tmp_path):
    path = tmp_path / "run-2026-08-26.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("pid=999999999 at=x start=Tue Jan  1 00:00:00 2001\n")
    with RunLock("2026-08-26", tmp_path):
        pass                       # reclaimed without raising


def test_a_recycled_pid_does_not_look_alive(tmp_path):
    """pid alone is not an identity. Our own pid with somebody else's start time
    is a recycled pid, and must read as dead."""
    holder = f"pid={os.getpid()} at=x start=Tue Jan  1 00:00:00 2001"
    assert holder_alive(holder) is False


def test_an_unreadable_probe_counts_as_alive(monkeypatch):
    """A failed `ps` is not a death certificate."""
    monkeypatch.setattr(locking, "_proc_start", lambda pid: None)
    holder = f"pid={os.getpid()} at=x start=Tue Jan  1 00:00:00 2001"
    assert holder_alive(holder) is True


def test_an_unidentifiable_lock_is_held_until_it_is_stale(tmp_path):
    path = tmp_path / "run-2026-08-26.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("written by something that named no pid\n")

    with pytest.raises(LockHeld):
        with RunLock("2026-08-26", tmp_path, stale_seconds=3600):
            pass

    old = time.time() - 7200
    os.utime(path, (old, old))
    with RunLock("2026-08-26", tmp_path, stale_seconds=3600):
        pass                       # now stale enough to reclaim


def test_exit_does_not_delete_a_lock_that_is_no_longer_ours(tmp_path):
    """If this run was wrongly declared stale and another run took the lock,
    deleting it on exit would hand a third run a free pass."""
    lock = RunLock("2026-08-26", tmp_path)
    lock.__enter__()
    lock.path.write_text("pid=1 at=someone-else start=x\n")
    lock.__exit__()
    assert lock.path.exists()


def test_losing_the_reclaim_race_reports_contention_not_a_disk_fault(tmp_path,
                                                                     monkeypatch):
    """Another run can win the gap between the unlink and the re-open. That is
    contention. A bare FileExistsError is an OSError, and the pipeline's OSError
    handler would tell the operator to go check their disk."""
    path = tmp_path / "run-2026-08-26.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("pid=999999999 at=x start=Tue Jan  1 00:00:00 2001\n")

    real = RunLock._open
    calls = {"n": 0}

    def racing_open(self):
        calls["n"] += 1
        if calls["n"] == 2:                 # the re-open after the reclaim
            raise FileExistsError(17, "File exists")
        return real(self)

    monkeypatch.setattr(RunLock, "_open", racing_open)
    with pytest.raises(LockHeld) as exc:
        with RunLock("2026-08-26", tmp_path):
            pass
    assert "while this one was reclaiming it" in str(exc.value)
    assert calls["n"] == 2
