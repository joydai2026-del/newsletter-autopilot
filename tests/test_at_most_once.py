"""At-most-once publishing: the property the whole repo exists to hold.

Every test here is a way the system could send twice, or send nothing without
saying so.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ISSUE_DATE, make_pipe

from newsletter_autopilot import pipeline as pipeline_mod
from newsletter_autopilot.adapters.mock import MockPublisher
from newsletter_autopilot.errors import Parked, StateError
from newsletter_autopilot.locking import RunLock
from newsletter_autopilot.notify import ConsoleNotifier
from newsletter_autopilot.pipeline import Pipeline
from newsletter_autopilot.state import CUTOFF_PARK_REASON, RunState


def test_happy_path_schedules_once(pipe, stories):
    res = pipe.publish(ISSUE_DATE, stories)
    assert res.ok and res.status == "scheduled"
    assert res.stages == ["ingest", "draft", "cover", "reconcile", "schedule",
                          "verify", "ledger"]
    assert res.trigger_at == "2026-08-26T13:00:00Z"       # 09:00 ET in UTC


def test_a_second_run_for_the_same_issue_is_a_no_op(pipe, stories, adapter):
    first = pipe.publish(ISSUE_DATE, stories)
    second = pipe.publish(ISSUE_DATE, stories)
    assert second.status == "already_scheduled"
    assert second.draft_id == first.draft_id
    assert adapter.calls.count("create_draft") == 1


def test_a_fresh_state_dir_still_cannot_double_send(cfg, adapter, stories):
    """The durable state is a fast path, not the safety property. Reconcile
    against the platform is what actually stops a second issue."""
    make_pipe(adapter, cfg).publish(ISSUE_DATE, stories)
    for f in cfg.state_dir.glob("*"):
        f.unlink()
    res = make_pipe(adapter, cfg).publish(ISSUE_DATE, stories)
    assert not res.ok and res.status == "parked"
    assert "already scheduled" in res.detail
    assert adapter.calls.count("create_draft") == 1


def test_an_already_published_issue_blocks_a_second_one(cfg, adapter, stories):
    """A post that already WENT OUT is not in the scheduled list, so reconcile
    has to ask the public feed too."""
    first = make_pipe(adapter, cfg).publish(ISSUE_DATE, stories)
    adapter._simulate_platform_firing(first.draft_id)
    for f in cfg.state_dir.glob("*"):
        f.unlink()
    # Same day, before the cutoff, so the run gets all the way to reconcile and
    # the public-feed question is the thing that has to catch it.
    res = make_pipe(adapter, cfg, when="06:00").publish(ISSUE_DATE, stories)
    assert res.status == "parked" and "already published" in res.detail
    assert adapter.calls.count("create_draft") == 1


def test_an_unreadable_reconcile_parks_rather_than_creating(cfg, stories):
    """An empty list is not proof of absence. An unreadable one certainly is not."""
    adapter = MockPublisher(cfg.home / "platform", empty_list_lie=True)
    res = make_pipe(adapter, cfg).publish(ISSUE_DATE, stories)
    assert res.status == "parked"
    assert "create_draft" not in adapter.calls


def test_an_ambiguous_schedule_parks_and_never_retries(cfg, stories):
    adapter = MockPublisher(cfg.home / "platform", ambiguous_on={"schedule"})
    res = make_pipe(adapter, cfg).publish(ISSUE_DATE, stories)
    assert res.status == "parked" and "ambiguous" in res.detail

    st = RunState.load(ISSUE_DATE, cfg.state_dir)
    assert st.phase == "parked"

    # A plain re-run must refuse. The server may already have scheduled it.
    adapter.ambiguous_on = set()
    again = make_pipe(adapter, cfg).publish(ISSUE_DATE, stories)
    assert again.status == "parked" and "do not write a script" in again.detail


def test_a_park_that_followed_a_remote_call_cannot_be_unparked(cfg, stories):
    adapter = MockPublisher(cfg.home / "platform", ambiguous_on={"schedule"})
    make_pipe(adapter, cfg).publish(ISSUE_DATE, stories)
    st = RunState.load(ISSUE_DATE, cfg.state_dir)
    with pytest.raises(Parked) as exc:
        st.unpark("wishful thinking")
    assert "not proven" in str(exc.value)


def test_state_refuses_to_move_backwards(cfg):
    st = RunState(issue_date=ISSUE_DATE, state_dir=cfg.state_dir)
    st.advance("drafted")
    st.advance("schedule_pending")
    with pytest.raises(StateError):
        st.advance("drafted")


def test_a_silent_no_op_write_is_caught_by_read_back(cfg, stories):
    """A success response is not proof of a write."""
    adapter = MockPublisher(cfg.home / "platform", silent_no_op_on={"set_cover"})
    res = make_pipe(adapter, cfg).publish(ISSUE_DATE, stories)
    assert not res.ok
    assert "cover did not attach" in res.detail
    assert "schedule" not in adapter.calls


def test_the_cutoff_parks_without_contacting_anything(cfg, adapter, stories):
    res = make_pipe(adapter, cfg, when="08:55").publish(ISSUE_DATE, stories)
    assert res.status == "parked" and res.detail == CUTOFF_PARK_REASON
    assert adapter.calls == []                    # nothing remote at all


def test_recovery_picks_up_a_cutoff_park_and_still_schedules(cfg, adapter, stories):
    make_pipe(adapter, cfg, when="08:55").publish(ISSUE_DATE, stories)
    res = make_pipe(adapter, cfg, when="10:30").publish(ISSUE_DATE, stories,
                                                       recovery=True)
    assert res.ok and res.status == "scheduled"
    # Released BY SCHEDULING, minutes out. Never an immediate-publish call.
    assert res.trigger_at == "2026-08-26T14:35:00Z"      # 10:35 ET


def test_recovery_refuses_a_second_release_even_after_the_phase_is_reset(
        cfg, adapter, stories):
    """The scenario that produced the original incident: an operator "clears"
    the run so the pipeline will try again. The phase resets, the audit trail
    does not, and the recorded release is what refuses."""
    make_pipe(adapter, cfg, when="08:55").publish(ISSUE_DATE, stories)
    make_pipe(adapter, cfg, when="10:30").publish(ISSUE_DATE, stories, recovery=True)

    st = RunState.load(ISSUE_DATE, cfg.state_dir)
    assert st.recovery_released() is not None
    st.phase = "selected"                        # the hand-edit
    st.save()

    again = make_pipe(adapter, cfg, when="11:00").publish(ISSUE_DATE, stories,
                                                          recovery=True)
    assert not again.ok
    assert "already released" in again.detail
    assert adapter.calls.count("create_draft") == 1


def test_recovery_attempts_are_bounded(cfg, stories):
    """Fail-closed, but WITHOUT burning the date on a failure that changed
    nothing: the marker is written only immediately before the first mutating
    call."""
    adapter = MockPublisher(cfg.home / "platform", ambiguous_on={"upload_image"})
    for _ in range(3):
        st = RunState.load_or_create(ISSUE_DATE, cfg.state_dir)
        if st.phase == "parked":
            st.phase = st.pre_park_phase or "selected"
            st.park_reason = None
            st.save()
        make_pipe(adapter, cfg, when="10:30").publish(ISSUE_DATE, stories,
                                                      recovery=True)
    st = RunState.load(ISSUE_DATE, cfg.state_dir)
    assert st.recovery_attempts() == 3
    st.phase, st.park_reason = "selected", None
    st.save()
    res = make_pipe(adapter, cfg, when="10:35").publish(ISSUE_DATE, stories,
                                                        recovery=True)
    assert "A further retry is not the answer" in res.detail


def test_a_read_failure_before_any_write_leaves_the_date_retryable(cfg, stories):
    adapter = MockPublisher(cfg.home / "platform", fail_on={"verify_identity"})
    make_pipe(adapter, cfg, when="10:30").publish(ISSUE_DATE, stories, recovery=True)
    st = RunState.load(ISSUE_DATE, cfg.state_dir)
    assert st.recovery_attempts() == 0            # nothing was burned


def test_the_audit_trail_survives_a_concurrent_writer(cfg):
    """A command that loads a snapshot, works slowly, then saves must not erase
    the at-most-once marker another run wrote meanwhile."""
    a = RunState.load_or_create(ISSUE_DATE, cfg.state_dir)
    a.record("first")
    b = RunState.load(ISSUE_DATE, cfg.state_dir)          # snapshot
    a.record("recovery_outcome", status="scheduled")      # the other run
    b.record("slow-command-finished")                     # stale snapshot saves
    events = [e["event"] for e in RunState.load(ISSUE_DATE, cfg.state_dir).events]
    assert "recovery_outcome" in events


def test_an_unexpected_internal_error_still_notifies(cfg, adapter, stories,
                                                     monkeypatch):
    """An uncaught exception inside the lock would leave the run as a traceback
    with nobody told, which is the exact silent failure the design exists to
    prevent."""
    from newsletter_autopilot import pipeline as pipeline_mod

    notifier = ConsoleNotifier()
    pipe = Pipeline(adapter, cfg, notifier,
                    clock=make_pipe(adapter, cfg)._clock)

    def boom(self, st, issue_date, *, dry_run=False):
        raise StateError("something nobody anticipated")

    monkeypatch.setattr(pipeline_mod.Pipeline, "_reconcile", boom)
    res = pipe.publish(ISSUE_DATE, stories)
    assert not res.ok
    assert "StateError" in res.detail
    assert notifier.sent, "the operator must be told, always"
    assert "NOT scheduled" in notifier.sent[-1][0]


def test_an_unreadable_scheduled_list_parks_on_a_plain_failure_too(cfg, stories):
    """Not just on Ambiguous: if the scheduled list cannot be read at all, there
    is no way to prove a post for this date does not already exist."""
    adapter = MockPublisher(cfg.home / "platform", fail_on={"scheduled_on"})
    res = make_pipe(adapter, cfg).publish(ISSUE_DATE, stories)
    assert res.status == "parked"
    assert "create_draft" not in adapter.calls


def test_a_bare_save_never_erases_a_concurrent_recovery_marker(cfg, adapter, stories):
    """The severe one. save() writes this object's in-memory events straight over
    the file, so any bare save() on a stale snapshot silently drops whatever
    another run appended, and losing the recovery_outcome marker is what permits
    a SECOND issue. The pipeline must reach the trail only through record()."""
    src = Path(pipeline_mod.__file__).read_text("utf8")
    body = src[src.index("def publish("):src.index("    # --- guarded sub-steps")]
    assert "st.save()" not in body, "publish() must use record(), never a bare save()"

    # And prove the hazard is real, so the assertion above is not folklore.
    a = RunState.load_or_create(ISSUE_DATE, cfg.state_dir)
    a.record("first")
    stale = RunState.load(ISSUE_DATE, cfg.state_dir)          # snapshot
    a.record("recovery_outcome", status="scheduled")           # the other run
    stale.save()                                               # a bare save() would...
    assert RunState.load(ISSUE_DATE, cfg.state_dir).recovery_released() is None
    # ...whereas record() merges the trail first.
    a2 = RunState.load_or_create(ISSUE_DATE, cfg.state_dir)
    a2.record("recovery_outcome", status="scheduled")
    stale2 = RunState.load(ISSUE_DATE, cfg.state_dir)
    stale2.record("slow-command-finished")
    assert RunState.load(ISSUE_DATE, cfg.state_dir).recovery_released() is not None


def test_a_dry_run_never_parks_the_real_issue(cfg, stories):
    """A command called "dry run" must not take the day down. Parking is terminal
    and needs a human, so a dry run that parked would brick the real send."""
    adapter = MockPublisher(cfg.home / "platform", empty_list_lie=True)
    res = make_pipe(adapter, cfg).publish(ISSUE_DATE, stories, dry_run=True)
    assert res.status == "dry_run"
    st = RunState.load(ISSUE_DATE, cfg.state_dir)
    assert st is None or st.phase != "parked"

    # The real publish afterwards is still free to run.
    adapter.empty_list_lie = False
    real = make_pipe(adapter, cfg).publish(ISSUE_DATE, stories)
    assert real.ok and real.status == "scheduled"


def test_a_dry_run_past_the_cutoff_reports_without_parking(cfg, adapter, stories):
    res = make_pipe(adapter, cfg, when="08:55").publish(ISSUE_DATE, stories,
                                                        dry_run=True)
    assert res.status == "dry_run"
    st = RunState.load(ISSUE_DATE, cfg.state_dir)
    assert st is None or st.phase != "parked"
    assert adapter.calls == []


def test_cancel_refuses_while_a_run_holds_the_lock(cfg, adapter, stories):
    """cancel() mutates the platform and the state file, so it is the realistic
    concurrent writer. It must take the same lock publish() takes."""
    make_pipe(adapter, cfg).publish(ISSUE_DATE, stories)
    pipe = make_pipe(adapter, cfg)
    with RunLock(ISSUE_DATE, cfg.state_dir, cfg.policy.stale_lock_seconds):
        res = pipe.cancel(ISSUE_DATE)
    assert not res.ok
    assert "right now" in res.detail
    assert "unschedule" not in adapter.calls


def test_cancel_works_when_no_run_holds_the_lock(cfg, adapter, stories):
    make_pipe(adapter, cfg).publish(ISSUE_DATE, stories)
    res = make_pipe(adapter, cfg).cancel(ISSUE_DATE)
    assert res.ok and "unschedule" in adapter.calls


def test_the_failure_message_admits_a_draft_exists(cfg, stories):
    """It used to state "nothing was created on the platform" on a path where a
    draft demonstrably exists, and the action line then contradicted it."""
    adapter = MockPublisher(cfg.home / "platform", silent_no_op_on={"set_cover"})
    notifier = ConsoleNotifier()
    Pipeline(adapter, cfg, notifier,
             clock=make_pipe(adapter, cfg)._clock).publish(ISSUE_DATE, stories)
    body = notifier.sent[-1][1]
    assert "A draft exists on the platform" in body
    assert "Nothing was created on the platform" not in body
    assert (adapter.root / "posts" / "d0001.json").exists()   # it really does
