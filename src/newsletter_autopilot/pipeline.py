"""The orchestrator: seven stages, one irreversible one, guards around it.

    1. ingest        read the story queue, drop anything already used
    2. draft         build and validate the issue document (no remote calls)
    3. cover         render the cover and prove it is not blank
    4. reconcile     ask the platform what already exists for this date
    5. schedule      THE IRREVERSIBLE STEP: set the send, confirm by read-back
    6. verify        prove a reader can actually see it
    7. ledger        record what this issue used, so it is never reused

Stages 1 to 4 are all reads and local work. Everything that could reject the run
happens there, on purpose, so a bad input costs zero contact with the platform.

The shape of the guard around stage 5 is the whole design:

    take an exclusive lock for this issue date
    if the state says we are already past 'scheduled'  -> no-op, exit 0
    if the state is parked                             -> refuse, exit 1
    if we are inside the cutoff window                 -> park, notify, exit 1
    reconcile scheduled AND published for this date    -> park if anything exists
    write the phase BEFORE the call, confirm AFTER
    ambiguous outcome                                  -> park, never retry
    after the schedule lands, NOTHING can fail the run: later problems are
    reported as "it IS scheduled, but ..." because at that point it cannot be
    stopped

`recovery=True` is the same path with a different trigger time and an
at-most-once marker in the durable state. It exists because three separate times
an operator wanted the issue out after the cutoff, found the supported path
refusing with no supported alternative, and hand-wrote a script that called the
platform's immediate-publish endpoint. One of those runs unscheduled a correct
future send and mailed the issue the night before. A refusal with no supported
next move is what produces the bypass, so the next move lives here, inside the
audited code, with its own guards. It still releases BY SCHEDULING, minutes out.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ledger as ledgers
from . import notify as notifications
from .adapters.base import PublisherAdapter
from .config import Config
from .cover import render_cover, write_cover
from .errors import (
    Ambiguous,
    AutopilotError,
    LockHeld,
    NotAuthenticated,
    Parked,
    PublisherError,
)
from .issue import (
    Issue,
    Story,
    build_issue,
    headlines,
    load_stories,
    select_stories,
)
from .locking import RunLock
from .notify import ConsoleNotifier, Notifier
from .state import CUTOFF_PARK_REASON, RunState


@dataclass
class Result:
    """What a run did, in terms the caller and the tests can both assert on."""

    ok: bool
    status: str                 # scheduled | already_scheduled | parked | refused | dry_run
    issue_date: str
    detail: str = ""
    draft_id: str | None = None
    trigger_at: str | None = None
    stages: list[str] = None    # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.stages is None:
            self.stages = []

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1


class Pipeline:
    def __init__(self, adapter: PublisherAdapter, cfg: Config | None = None,
                 notifier: Notifier | None = None, *, clock=None):
        self.adapter = adapter
        self.cfg = cfg or Config()
        self.notifier = notifier or ConsoleNotifier()
        # Injectable clock. The cutoff logic is the part most worth testing and
        # the part least testable against a real wall clock.
        self._clock = clock or (lambda: dt.datetime.now(self.cfg.policy.tz))

    # --- clock helpers ----------------------------------------------------

    def now(self) -> dt.datetime:
        return self._clock().astimezone(self.cfg.policy.tz)

    def past_cutoff(self, issue_date: str) -> bool:
        slot = self.cfg.policy.slot_on(issue_date)
        margin = dt.timedelta(minutes=self.cfg.policy.cutoff_minutes)
        return self.now() >= slot - margin

    def recovery_trigger(self) -> dt.datetime:
        return self.now() + dt.timedelta(minutes=self.cfg.policy.recovery_lead_minutes)

    # --- stages 1 to 3 (local only) --------------------------------------

    def prepare(self, issue_date: str, stories_path: Path | str, *,
                title: str | None = None) -> tuple[Issue, bytes, Path]:
        """Ingest, draft, and render. Contacts nothing. Raises DraftRejected or
        RenderGateFailed before a single remote byte."""
        stories: list[Story] = load_stories(stories_path)
        fresh = self._drop_used(stories, issue_date)
        picked = select_stories(fresh, self.cfg)
        issue = build_issue(issue_date, picked, self.cfg, title=title)

        png = render_cover(issue_date, issue.title)
        cover_path = write_cover(self.cfg.output_dir / f"{issue_date}-cover.png", png)
        return issue, png, cover_path

    def _drop_used(self, stories: list[Story], issue_date: str) -> list[Story]:
        """Drop stories a PREVIOUS issue already used.

        The exclusion of THIS issue's own date is not a detail, it is what makes
        a resumed run reproduce the same issue. The ledger is written after the
        schedule lands, so any run that resumes after that point would otherwise
        find its own five stories marked used, rebuild a different issue, and
        either fail validation or, worse, seal different content onto a draft
        that is already scheduled. A dedup filter that is not date-aware turns
        an idempotent resume into a content swap.
        """
        led = ledgers.Ledger("stories", self.cfg.ledger_dir)
        used = led.all_entries() - set(led.entries_for(issue_date))
        return [s for s in stories if s.url not in used]

    # --- stages 4 to 7 (the guarded part) --------------------------------

    def publish(self, issue_date: str, stories_path: Path | str, *,
                dry_run: bool = False, recovery: bool = False,
                title: str | None = None) -> Result:
        stages: list[str] = []
        cfg, pol = self.cfg, self.cfg.policy

        if not pol.is_publishing_day(issue_date) and not recovery:
            return Result(False, "refused", issue_date,
                          f"{issue_date} is not a publishing day under this policy")

        # --- 1 to 3, before the lock, before anything remote --------------
        try:
            issue, cover_png, cover_path = self.prepare(issue_date, stories_path,
                                                        title=title)
            stages += ["ingest", "draft", "cover"]
        except AutopilotError as exc:
            notifications.failed(
                self.notifier, issue_date=issue_date, phase="building the issue",
                reason=str(exc),
                action="fix the story queue or the draft, then re-run")
            return Result(False, "refused", issue_date, str(exc), stages=stages)

        # The trigger and the issue must be the same local day, always. Checked
        # here, before any remote call, and asserted again immediately before the
        # schedule write. An issue never rolls into another day.
        trigger_local = self.recovery_trigger() if recovery else pol.slot_on(issue_date)
        if trigger_local.astimezone(pol.tz).date().isoformat() != issue_date:
            msg = (f"the computed trigger {trigger_local:%Y-%m-%d %H:%M} is not on "
                   f"issue date {issue_date}. Refusing.")
            notifications.failed(self.notifier, issue_date=issue_date,
                                 phase="checking the release clock", reason=msg,
                                 action="stop and tell a human. Nothing was contacted.")
            return Result(False, "refused", issue_date, msg, stages=stages)

        made_draft = False          # in-memory flags, NOT re-read from disk in the
        reached_schedule = False    # handlers: if the disk is what failed, a handler
        url = ""                    # that reads it raises and nobody gets told

        try:
            with RunLock(issue_date, cfg.state_dir, pol.stale_lock_seconds):
                st = RunState.load_or_create(issue_date, cfg.state_dir)

                # Parked is checked FIRST. reached() answers False for a parked
                # run, so checking it later made the recovery unreachable in the
                # one state it exists for: publish parks at the cutoff, then the
                # recovery refused the park.
                if st.phase == "parked":
                    if recovery and st.park_reason == CUTOFF_PARK_REASON:
                        st.unpark("recovery picking up a cutoff park (nothing remote "
                                  "had happened)")
                    else:
                        return Result(
                            False, "parked", issue_date,
                            f"the run for {issue_date} is parked: {st.park_reason}. "
                            "This park followed a remote call, so what is on the "
                            "platform is not proven. Nothing was contacted. Stop and "
                            "report this; do not write a script.", stages=stages)

                if st.reached("scheduled"):
                    return Result(True, "already_scheduled", issue_date,
                                  f"already scheduled (draft {st.draft_id})",
                                  draft_id=st.draft_id,
                                  trigger_at=st.scheduled_trigger_at, stages=stages)

                if recovery:
                    guard = self._recovery_guard(st)
                    if guard is not None:
                        return Result(False, "refused", issue_date, guard, stages=stages)
                elif self.past_cutoff(issue_date):
                    # A dry run REPORTS the cutoff; it must never park the date.
                    # Parking is terminal and needs a human, so a command named
                    # "dry run" would brick the real issue it was only inspecting.
                    if not dry_run:
                        st.park(CUTOFF_PARK_REASON)
                        notifications.failed(
                            self.notifier, issue_date=issue_date,
                            phase="the cutoff check",
                            reason=f"the run reached publishing inside the "
                                   f"{pol.cutoff_minutes}-minute margin before the "
                                   f"{issue_date} {pol.publish_at} slot, so it made no "
                                   "changes on the platform at all.",
                            action="run the recovery command if you still want this "
                                   "issue out today")
                    return Result(False, "parked" if not dry_run else "dry_run",
                                  issue_date, CUTOFF_PARK_REASON, stages=stages)

                # record(), never a bare save(). save() writes this object's
                # in-memory events straight over the file, so it would silently
                # drop anything a concurrent writer appended meanwhile, including
                # the recovery_outcome marker whose loss permits a second issue.
                # record() re-reads and merges the trail first.
                st.source_ids = issue.source_ids
                st.source_urls = issue.source_urls
                st.record("sources_selected", count=len(issue.source_ids))

                self.adapter.verify_identity()

                # --- 4. reconcile ----------------------------------------
                blocked = self._reconcile(st, issue_date, dry_run=dry_run)
                if blocked is not None:
                    return Result(False, "parked" if not dry_run else "dry_run",
                                  issue_date, blocked, stages=stages)
                stages.append("reconcile")

                if dry_run:
                    return Result(True, "dry_run", issue_date,
                                  f"would schedule {issue.title!r} for "
                                  f"{trigger_local:%Y-%m-%d %H:%M %Z}; cover at "
                                  f"{cover_path}",
                                  trigger_at=trigger_local.isoformat(), stages=stages)

                if recovery:
                    # Written HERE: the last statement before the first mutating
                    # call. Everything above is a read, so a failure up there
                    # costs nothing and the date stays retryable.
                    st.record("recovery_attempted",
                              attempt=st.recovery_attempts() + 1,
                              trigger_local=trigger_local.isoformat())

                # --- 5. schedule -----------------------------------------
                cover_url = self.adapter.upload_image(cover_png, cover_path.name)
                draft_id = self._draft(st, issue)
                made_draft = True
                self._set_cover_confirmed(draft_id, cover_url)
                self._seal_content(draft_id, issue)

                # Last check before the line that emails people.
                if trigger_local.astimezone(pol.tz).date().isoformat() != issue_date:
                    raise PublisherError(
                        f"refusing to schedule: the trigger {trigger_local.isoformat()} "
                        f"is not on issue date {issue_date}")

                trigger_utc = trigger_local.astimezone(dt.UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                reached_schedule = True
                st.advance("schedule_pending", trigger_at=trigger_utc)
                self.adapter.schedule(draft_id, trigger_utc, audience="everyone")
                self._confirm_schedule(draft_id, trigger_utc)
                st.scheduled_trigger_at = trigger_utc
                st.advance("scheduled", trigger_at=trigger_utc)
                if recovery:
                    # The outcome half of the at-most-once marker. Its ABSENCE is
                    # what tells a later run "an attempt may have left something
                    # behind"; its presence makes a second release refuse outright.
                    st.record("recovery_outcome", status="scheduled",
                              trigger_at=trigger_utc, draft_id=draft_id)
                stages.append("schedule")
                url = f"{self.adapter.name}://post/{draft_id}"

                # === past this line, nothing can unsend the issue ==========

                # --- 6. verify -------------------------------------------
                try:
                    render = self.adapter.public_render(draft_id)
                    st.record("public_render", **{k: render.get(k) for k in
                                                  ("status", "body_html_len")
                                                  if k in render})
                    stages.append("verify")
                except (Ambiguous, PublisherError) as exc:
                    notifications.scheduled_but_incomplete(
                        self.notifier, issue_date=issue_date, url=url,
                        problem=f"scheduled, but the render check failed ({exc}). Do "
                                "not create a second post.")

                # --- 7. ledger -------------------------------------------
                # A ledger failure never stops or re-runs anything: the post is
                # already scheduled, and the only cost is a possible repeat.
                try:
                    ledgers.record_issue(cfg.ledger_dir, issue_date, issue.assets())
                    st.record("ledgers_recorded")
                    stages.append("ledger")
                except (ledgers.LedgerError, OSError) as exc:
                    st.record("ledgers_failed", error=str(exc)[:200])
                    notifications.scheduled_but_incomplete(
                        self.notifier, issue_date=issue_date, url=url,
                        problem=f"the dedup ledgers were not updated ({exc}), so an "
                                "asset from today could be picked again.")

                st.advance("sources_marked", marked=len(issue.source_ids))
                notifications.scheduled(
                    self.notifier, issue_date=issue_date, title=issue.title,
                    headlines=headlines(issue),
                    when_local=trigger_local.strftime("%A %d %b, %H:%M %Z"), url=url)
                st.advance("complete")
                return Result(True, "scheduled", issue_date,
                              f"scheduled for {trigger_local:%Y-%m-%d %H:%M %Z}",
                              draft_id=draft_id, trigger_at=trigger_utc, stages=stages)

        except LockHeld as exc:
            # This used to only print. Under a scheduled task nobody reads stdout,
            # so a wedged lock meant the newsletter silently did not go out, day
            # after day, with no signal at all.
            notifications.failed(
                self.notifier, issue_date=issue_date,
                phase="waiting for another run to finish", reason=str(exc),
                action=f"if no other run is going, delete "
                       f"{cfg.state_dir / f'run-{issue_date}.lock'} and re-run")
            return Result(False, "refused", issue_date, str(exc), stages=stages)

        except NotAuthenticated as exc:
            notifications.failed(self.notifier, issue_date=issue_date,
                                 phase="signing in to the platform", reason=str(exc),
                                 action="refresh the credential, then re-run")
            return Result(False, "refused", issue_date, str(exc), stages=stages)

        except Ambiguous as exc:
            # NOTIFY FIRST, park second. A handler that writes state before
            # sending will, on a disk failure, raise and send nothing, on exactly
            # the path where the email may already be scheduled. The message is
            # what has to survive; persisting the park is best effort.
            if made_draft or reached_schedule:
                notifications.scheduled_but_incomplete(
                    self.notifier, issue_date=issue_date, url=url, problem=str(exc))
            else:
                notifications.failed(
                    self.notifier, issue_date=issue_date,
                    phase="talking to the platform", reason=str(exc),
                    created=made_draft,
                    action="check the platform by hand before re-running; do NOT "
                           "just re-run")
            try:
                RunState.load_or_create(issue_date, cfg.state_dir).park(
                    f"ambiguous outcome: {exc}")
            except OSError:
                pass
            return Result(False, "parked", issue_date, f"ambiguous: {exc}", stages=stages)

        except (PublisherError, Parked) as exc:
            notifications.failed(self.notifier, issue_date=issue_date,
                                 phase="publishing", reason=str(exc),
                                 created=made_draft,
                                 action="read the error, then re-run")
            return Result(False, "refused", issue_date, str(exc), stages=stages)

        except OSError as exc:
            # A local disk failure. WHERE it died decides what is honest to say:
            # reporting a post-schedule disk error as "fix your files and re-run"
            # would tell someone to retry an issue that is already going out.
            # Deliberately NOT re-reading state here; the disk is what failed.
            if reached_schedule:
                notifications.scheduled_but_incomplete(
                    self.notifier, issue_date=issue_date, url=url,
                    problem=f"a local disk error happened around the schedule call "
                            f"({exc}). The post may well BE scheduled. Check before "
                            "doing anything.")
                return Result(False, "parked", issue_date, f"disk error: {exc}",
                              stages=stages)
            notifications.failed(
                self.notifier, issue_date=issue_date, phase="writing local state",
                reason=str(exc), created=made_draft,
                action="check the disk, then re-run")
            return Result(False, "refused", issue_date, f"disk error: {exc}",
                          stages=stages)

        except AutopilotError as exc:
            # The catch-all, and it is not defensive padding. Without it a
            # StateError or a ConfigError raised anywhere inside the lock leaves
            # this function as an uncaught traceback and NOBODY IS TOLD, which is
            # precisely the silent failure the whole design exists to prevent.
            # Whether it is honest to say "nothing went out" depends on where it
            # died, so ask the in-memory flag, never the disk.
            if reached_schedule:
                notifications.scheduled_but_incomplete(
                    self.notifier, issue_date=issue_date, url=url,
                    problem=f"an unexpected error happened after the schedule call "
                            f"({type(exc).__name__}: {exc}). The post may well BE "
                            "scheduled. Check before doing anything.")
                return Result(False, "parked", issue_date,
                              f"post-schedule error: {exc}", stages=stages)
            notifications.failed(
                self.notifier, issue_date=issue_date, phase="running the pipeline",
                reason=f"{type(exc).__name__}: {exc}", created=made_draft,
                action="this is a bug, not a configuration problem. Nothing was "
                       "scheduled. Report it with this message.")
            return Result(False, "refused", issue_date,
                          f"{type(exc).__name__}: {exc}", stages=stages)

    # --- guarded sub-steps ------------------------------------------------

    def _recovery_guard(self, st: RunState) -> str | None:
        """At most once SUCCESSFULLY, enforced through durable state rather than
        an operator's memory, and fail-closed WITHOUT burning the date on a
        failure that changed nothing."""
        released = st.recovery_released()
        if released:
            return (f"a recovery for {st.issue_date} already released this issue at "
                    f"{released.get('at')}. Nothing was contacted. Stop and report "
                    "this; do not write a script.")
        attempts = st.recovery_attempts()
        if attempts >= self.cfg.policy.max_recovery_attempts:
            return (f"{attempts} recovery attempts for {st.issue_date} have run "
                    "without a confirmed release. A further retry is not the answer. "
                    "Nothing was contacted.")
        return None

    def _reconcile(self, st: RunState, issue_date: str, *,
                   dry_run: bool = False) -> str | None:
        """Ask the platform what already exists for this date. Two questions, not
        one: a post that ALREADY WENT OUT is not in the scheduled list.

        `dry_run` suppresses the park and the alert, never the check. Parking is
        terminal and needs a human, so a read-only inspection that parked the
        date would take the day's issue down and page somebody, which is the
        opposite of what "dry run" promises. The verdict is still returned.
        """
        def stop(verdict: str, **msg) -> str:
            if not dry_run:
                st.park(verdict)
                notifications.failed(self.notifier, issue_date=issue_date, **msg)
            return verdict
        try:
            existing = self.adapter.scheduled_on(issue_date)
        except (Ambiguous, PublisherError) as exc:
            return stop(
                f"the scheduled list could not be read: {exc}", phase="reconcile",
                reason=f"the platform's scheduled list could not be established "
                       f"({exc}), so there is no way to prove a post for this date "
                       "does not already exist. Nothing was created.",
                action="look at the publication dashboard, then stop. Do not re-run "
                       "blind.")
        if existing:
            return stop(
                "already scheduled on the platform", phase="reconcile",
                reason=f"the platform already has {len(existing)} post(s) scheduled "
                       f"for {issue_date}. Nothing was created, to avoid sending two "
                       "issues.",
                action="cancel the duplicate on the platform, then re-run")

        # A future date cannot have been published, so only ask when it could have.
        if issue_date <= self.now().date().isoformat():
            try:
                already = self.adapter.published_on(issue_date)
            except (Ambiguous, PublisherError) as exc:
                return stop(
                    f"public feed unreadable: {exc}",
                    phase="checking what is already published",
                    reason=f"the public feed could not be read ({exc}), so there is "
                           f"no way to prove the {issue_date} issue has not already "
                           "gone out. Nothing was created.",
                    action="look at the publication's public page, then stop.")
            if already:
                return stop(
                    "already published", phase="reconcile",
                    reason=f"the platform already has {len(already)} published post(s) "
                           f"dated {issue_date}, so the issue is already out. Nothing "
                           "was created: a second one would email the whole list twice.",
                    action="stop and tell a human. Do not create another post.")
        return None

    def _draft(self, st: RunState, issue: Issue) -> str:
        """Create the draft, or prove a recorded one is ours and reuse it.

        NEVER reuse an id on faith. A recorded id can point at a draft deleted by
        hand (every retry then dies on the dead id and the issue silently never
        goes out) or, if state were corrupted, at somebody else's post, which
        this would overwrite and mail out.
        """
        if st.reached("drafted") and st.draft_id:
            did = st.draft_id
            prior = self.adapter.get_draft(did)      # a read failure raises; caller parks
            if not st.created_title:
                raise PublisherError(
                    f"draft {did} was recorded before this run started recording which "
                    "title it created, so it cannot be proven to be ours. Nothing was "
                    "written to it.")
            if prior.get("title") != st.created_title:
                raise PublisherError(
                    f"draft {did} was created with the title {st.created_title!r} but "
                    f"now reads {str(prior.get('title'))[:60]!r}. Nothing was written "
                    "to it, because it may not be the right post.")
            if prior.get("published") or prior.get("trigger_at"):
                raise PublisherError(
                    f"draft {did} is already published or scheduled. Nothing was "
                    "changed, to avoid sending a second issue.")
            self.adapter.update_draft(did, title=issue.title, subtitle=issue.subtitle,
                                      body=issue.doc)
            self._seal_content(did, issue)
            st.record("draft_reused", draft_id=did)
            return did

        did = self.adapter.create_draft(title=issue.title, subtitle=issue.subtitle,
                                        body=issue.doc)
        st.draft_id = did
        st.created_title = issue.title
        st.advance("drafted", draft_id=did)
        return did

    def _set_cover_confirmed(self, draft_id: str, cover_url: str) -> None:
        self.adapter.set_cover(draft_id, cover_url)
        back = self.adapter.get_draft(draft_id)
        if back.get("cover_url") != cover_url:
            raise PublisherError(
                f"the cover did not attach to draft {draft_id}. The call returned "
                "success and the read-back disagrees, which is exactly why every "
                "mutation here is confirmed.")

    def _seal_content(self, draft_id: str, issue: Issue) -> None:
        """The content seal, immediately before the schedule.

        A schedule read-back only proves the trigger and audience. This proves the
        title, subtitle, and body actually landed, because a success response is
        not proof of a write and an un-applied body update would schedule the
        wrong content to a real list.
        """
        back = self.adapter.get_draft(draft_id)
        problems = [name for name, want, got in (
            ("title", issue.title, back.get("title")),
            ("subtitle", issue.subtitle, back.get("subtitle")),
            ("body", issue.doc, back.get("body")),
        ) if want != got]
        if problems:
            raise PublisherError(
                f"draft {draft_id} did not take {', '.join(problems)}; refusing to "
                "schedule content that may be stale")

    def _confirm_schedule(self, draft_id: str, trigger_utc: str) -> None:
        back = self.adapter.get_draft(draft_id)
        if back.get("trigger_at") != trigger_utc:
            raise Ambiguous(
                f"the schedule call returned success but draft {draft_id} reads back "
                f"with trigger {back.get('trigger_at')!r} instead of {trigger_utc!r}. "
                "The outcome is unknown; this run will not try again.")

    # --- cancel -----------------------------------------------------------

    def cancel(self, issue_date: str) -> Result:
        """Unschedule an issue that has not gone out. Requires an explicit date:
        there is no 'cancel whatever is next'."""
        # Under the SAME lock as publish(). This mutates the platform and the
        # state file, so an unlocked cancel is a real concurrent writer racing a
        # live run: it can unschedule a post that run just scheduled, and its
        # state write can clobber the run's audit trail.
        try:
            with RunLock(issue_date, self.cfg.state_dir,
                         self.cfg.policy.stale_lock_seconds):
                st = RunState.load(issue_date, self.cfg.state_dir)
                if st is None or not st.draft_id:
                    return Result(False, "refused", issue_date,
                                  "no recorded run for that date")
                try:
                    published = self.adapter.published_on(issue_date)
                except (Ambiguous, PublisherError) as exc:
                    return Result(False, "refused", issue_date,
                                  f"cannot prove the issue has not already gone out "
                                  f"({exc}); refusing to touch it")
                if published:
                    return Result(False, "refused", issue_date,
                                  "that issue has already gone out; it cannot be unsent")
                self.adapter.unschedule(st.draft_id)
                st.park(f"cancelled by hand on {self.now():%Y-%m-%d %H:%M}")
                return Result(True, "parked", issue_date,
                              f"unscheduled draft {st.draft_id}", draft_id=st.draft_id)
        except LockHeld as exc:
            return Result(False, "refused", issue_date,
                          f"a run is working on {issue_date} right now ({exc}); "
                          "refusing to cancel underneath it")

    def status(self, issue_date: str) -> dict[str, Any]:
        st = RunState.load(issue_date, self.cfg.state_dir)
        if st is None:
            return {"issue_date": issue_date, "phase": "none"}
        return {"issue_date": issue_date, "phase": st.phase, "draft_id": st.draft_id,
                "trigger_at": st.scheduled_trigger_at, "park_reason": st.park_reason,
                "events": len(st.events)}
