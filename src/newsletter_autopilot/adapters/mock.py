"""A filesystem-backed publisher that emails nobody.

This is the adapter the dry-run uses, and it is not a stub. It reproduces the
behaviours that make a real newsletter API dangerous, so the safety machinery
can be exercised and tested without a credential:

    fail_on={"schedule"}         a clean failure: provably did nothing
    ambiguous_on={"schedule"}    a timeout-shaped outcome: MAY have acted
    silent_no_op_on={"set_cover"}   returns success and does nothing (rule 1)
    empty_list_lie=True          reconcile reads come back suspiciously empty (rule 2)

Every "post" is a JSON file under the adapter's directory, so you can look at
exactly what a run produced.
"""

from __future__ import annotations

import datetime as dt
import json
import zoneinfo
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..errors import Ambiguous, NotAuthenticated, PublisherError
from .base import BaseAdapter


class MockPublisher(BaseAdapter):
    """An in-repo publishing platform. Writes files; sends nothing."""

    name = "mock"

    def __init__(self, root: Path | str, *,
                 publication: str = "Example Daily",
                 timezone: str = "America/New_York",
                 credential: str | None = "mock-credential",
                 fail_on: Iterable[str] = (),
                 ambiguous_on: Iterable[str] = (),
                 silent_no_op_on: Iterable[str] = (),
                 empty_list_lie: bool = False):
        self.root = Path(root)
        self.publication = publication
        self.tz = zoneinfo.ZoneInfo(timezone)
        self._credential = credential
        self.fail_on = set(fail_on)
        self.ambiguous_on = set(ambiguous_on)
        self.silent_no_op_on = set(silent_no_op_on)
        self.empty_list_lie = empty_list_lie
        self.calls: list[str] = []
        (self.root / "posts").mkdir(parents=True, exist_ok=True)
        (self.root / "media").mkdir(parents=True, exist_ok=True)

    # --- plumbing ---------------------------------------------------------

    def _gate(self, op: str) -> None:
        """Record the call and apply any injected fault."""
        self.calls.append(op)
        if op in self.ambiguous_on:
            raise Ambiguous(f"{op}: the request timed out; the outcome is unknown")
        if op in self.fail_on:
            raise PublisherError(f"{op}: the platform refused (nothing was changed)")

    def _post_path(self, draft_id: str) -> Path:
        return self.root / "posts" / f"{draft_id}.json"

    def _read(self, draft_id: str) -> dict[str, Any]:
        p = self._post_path(draft_id)
        if not p.exists():
            raise PublisherError(f"no draft {draft_id}")
        return json.loads(p.read_text(encoding="utf8"))

    def _write(self, post: dict[str, Any]) -> None:
        p = self._post_path(str(post["id"]))
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(post, indent=2, sort_keys=True), encoding="utf8")
        tmp.replace(p)

    def _all_posts(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted((self.root / "posts").glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf8")))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def _local_day(self, stamp: str) -> str:
        """The publication's own calendar day for a UTC timestamp.

        This conversion is not cosmetic. The platform stores UTC; the issue's
        identity is a local date. Comparing the raw UTC string against a local
        date agrees for a morning slot and DISAGREES for any evening release,
        which is precisely how an evening recovery becomes invisible to the one
        check that stops two issues going out.

        An unreadable timestamp raises rather than quietly answering "some other
        day", because treating an unreadable value as a non-match is the same
        mistake as treating an empty list as absence.
        """
        text = str(stamp or "").strip()
        if not text:
            raise Ambiguous("a post came back with no timestamp, so its date cannot "
                            "be established. Refusing to call it another day's post.")
        try:
            moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise Ambiguous(f"could not read the timestamp {text[:40]!r}, so its date "
                            "cannot be established.") from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.UTC)
        return moment.astimezone(self.tz).date().isoformat()

    # --- adapter surface --------------------------------------------------

    def verify_identity(self) -> str:
        self._gate("verify_identity")
        if not self._credential:
            raise NotAuthenticated(
                "No credential configured for the mock publisher. Pass "
                "credential=... when constructing it.")
        return self.publication

    def scheduled_on(self, date: str) -> list[dict[str, Any]]:
        self._gate("scheduled_on")
        if self.empty_list_lie:
            raise Ambiguous(
                "the scheduled list came back empty from a surface that is known to "
                "under-report. An empty list is not proof of absence, so this run "
                "will not create a post on the strength of it.")
        return [p for p in self._all_posts()
                if p.get("trigger_at") and not p.get("published")
                and self._local_day(p["trigger_at"]) == date]

    def published_on(self, date: str) -> list[dict[str, Any]]:
        self._gate("published_on")
        return [p for p in self._all_posts()
                if p.get("published") and self._local_day(p["published_at"]) == date]

    def upload_image(self, data: bytes, filename: str) -> str:
        self._gate("upload_image")
        safe = Path(filename).name or "image.bin"
        dest = self.root / "media" / safe
        dest.write_bytes(data)
        return f"mock://media/{safe}"

    def create_draft(self, *, title: str, subtitle: str, body: dict[str, Any]) -> str:
        self._gate("create_draft")
        draft_id = f"d{len(list((self.root / 'posts').glob('*.json'))) + 1:04d}"
        self._write({"id": draft_id, "title": title, "subtitle": subtitle,
                     "body": body, "cover_url": None, "trigger_at": None,
                     "published": False, "published_at": None,
                     "created_at": dt.datetime.now(dt.UTC).isoformat()})
        return draft_id

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        self._gate("get_draft")
        return self._read(draft_id)

    def update_draft(self, draft_id: str, **fields: Any) -> None:
        self._gate("update_draft")
        post = self._read(draft_id)
        if "update_draft" in self.silent_no_op_on:
            return                        # 200 OK, nothing written. Rule 1.
        post.update({k: v for k, v in fields.items() if k in
                     ("title", "subtitle", "body", "cover_url")})
        self._write(post)

    def set_cover(self, draft_id: str, image_url: str) -> None:
        self._gate("set_cover")
        if "set_cover" in self.silent_no_op_on:
            return                        # 200 OK, nothing written. Rule 1.
        post = self._read(draft_id)
        post["cover_url"] = image_url
        self._write(post)

    def schedule(self, draft_id: str, trigger_at_utc: str, *,
                 audience: str = "everyone") -> None:
        self._gate("schedule")
        post = self._read(draft_id)
        if post.get("trigger_at") or post.get("published"):
            raise PublisherError(f"draft {draft_id} is already scheduled or published")
        post["trigger_at"] = trigger_at_utc
        post["audience"] = audience
        self._write(post)

    def unschedule(self, draft_id: str) -> None:
        self._gate("unschedule")
        post = self._read(draft_id)
        if post.get("published"):
            raise PublisherError(f"draft {draft_id} has already gone out; it cannot "
                                 "be unscheduled")
        post["trigger_at"] = None
        self._write(post)

    def public_render(self, draft_id: str) -> dict[str, Any]:
        self._gate("public_render")
        post = self._read(draft_id)
        if not post.get("published"):
            return {"status": "deferred", "reason": "not public yet"}
        return {"status": "ok", "title": post.get("title"),
                "subtitle": post.get("subtitle"),
                "body_html_len": len(json.dumps(post.get("body") or {}))}

    # --- test-only helper -------------------------------------------------

    def _simulate_platform_firing(self, draft_id: str, *,
                                  when: str | None = None) -> None:
        """Stand in for the PLATFORM'S OWN scheduler reaching the trigger time.

        Named with a leading underscore and spelled out because it is the one
        method here that makes a post public, and it must never be mistaken for
        an adapter capability. It is not on the PublisherAdapter contract, the
        pipeline cannot reach it, and only tests and the demo call it.
        """
        post = self._read(draft_id)
        post["published"] = True
        post["published_at"] = when or post.get("trigger_at") or \
            dt.datetime.now(dt.UTC).isoformat()
        self._write(post)
