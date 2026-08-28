#!/usr/bin/env python3
"""Did an issue actually reach readers today? Alarm if not.

WHY THIS FILE IS STANDALONE, AND WHY THAT IS THE WHOLE POINT

The pipeline this was distilled from missed an issue in silence. The working
directory had been left on a feature branch overnight; the publisher's directory
does not exist on that branch, so the morning's scheduled run found no code,
built nothing, published nothing, and raised nothing. Every check that could
have caught it lived inside the directory that had vanished.

A check that can disappear along with the thing it checks is not a check.

So this file:

  * lives outside the pipeline's tree and is deployed separately,
  * imports NOTHING from the pipeline (standard library only), so it still runs
    when the pipeline is missing, broken, or half-installed,
  * asks the only question that cannot be faked: does the publication's own
    public archive contain a post published today?

Machinery-level signals all looked healthy on the day nothing shipped: the
scheduler fired, the exit code was 0, the log line was written. "The job ran" and
"readers got it" are different claims, and only the second one matters.

Exit codes
    0  an issue is out, or today is not a publishing day, or the slot has not
       passed yet
    1  nothing published, and the alarm has been raised
    2  the check itself could not run, and the alarm has been raised. A broken
       watchdog must never read as all clear.

Usage
    outcome_check.py --archive-url URL [--publish-hour 9] [--grace-minutes 60]
                     [--timezone America/New_York] [--webhook URL]
                     [--date-field post_date] [--now ISO8601]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

USER_AGENT = "newsletter-autopilot-outcome-check"


def fetch_archive(url: str, timeout: int = 30) -> list[dict]:
    """Read the public archive. Raises on anything it cannot understand."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    if isinstance(payload, dict):
        for key in ("posts", "items", "results", "data"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise RuntimeError(f"the archive did not return a list: {str(payload)[:120]}")
    return payload


def published_today(posts: list[dict], now: datetime, tz: ZoneInfo,
                    date_field: str,
                    title_contains: str | None = None) -> tuple[bool, str]:
    """(found, detail) for a post whose publish date is today in the local zone.

    The conversion to local time is load-bearing. Archives report UTC; an issue's
    identity is a local date. Comparing the raw UTC string against a local date
    agrees for a morning send and disagrees for an evening one.

    An unreadable timestamp is SKIPPED here, which is the opposite of what the
    pipeline's adapter does with the same input, and the difference is deliberate.
    The pipeline is deciding whether to create a post, so an unreadable answer
    must stop it. The watchdog is deciding whether to raise an alarm, so an
    unreadable answer must not suppress one: skipping pushes the verdict toward
    "not found", which alarms. Both choices err away from silence.
    """
    today = now.date().isoformat()
    for p in posts:
        raw = str(p.get(date_field) or "")
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=ZoneInfo("UTC"))
        when = when.astimezone(tz)
        if when.date().isoformat() != today:
            continue
        title = str(p.get("title") or "")
        if title_contains and title_contains.lower() not in title.lower():
            # Some other post went out today. Without this, a one-off
            # announcement masks a missed issue and the watchdog says all clear.
            continue
        return True, f"{when:%H:%M} {when.tzname()} - {title[:70]}"
    if not posts:
        return False, "(the archive came back empty)"
    # Reported for a human to read, not relied on: the archive's order is the
    # archive's business, so this says "first entry", not "newest".
    first = str(posts[0].get(date_field, "?"))
    return False, f"no post is dated today; the first archive entry reads {first}"


def alarm(message: str, webhook: str | None) -> None:
    """Best effort. A failure here is printed, never raised: the exit code still
    carries the verdict for whatever runs this."""
    print(message)
    if not webhook:
        print("NOTE: no --webhook given, so the alarm was printed only",
              file=sys.stderr)
        return
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps({"text": message}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            ok = 200 <= resp.status < 300
        print("alarm sent" if ok else "ALARM NOT SENT: the webhook rejected it",
              file=sys.stdout if ok else sys.stderr)
    except Exception as exc:            # noqa: BLE001 - the alarm must never crash the check
        print(f"ALARM NOT SENT: {type(exc).__name__}: {exc}", file=sys.stderr)


def check(*, archive_url: str, tz: ZoneInfo, publish_hour: int, publish_minute: int,
          grace_minutes: int, publish_days: set[int], date_field: str,
          now: datetime, webhook: str | None,
          title_contains: str | None = None) -> int:
    if now.weekday() not in publish_days:
        print(f"# outcome: {now:%Y-%m-%d} is not a publishing day, no issue expected")
        return 0
    slot = now.replace(hour=publish_hour, minute=publish_minute, second=0, microsecond=0)
    if now < slot + timedelta(minutes=grace_minutes):
        print(f"# outcome: it is only {now:%H:%M}, the send slot plus its grace "
              "period has not passed")
        return 0
    try:
        found, detail = published_today(fetch_archive(archive_url), now, tz,
                                        date_field, title_contains)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
        alarm(f"Outcome check COULD NOT RUN ({now:%Y-%m-%d}): {type(exc).__name__}: "
              f"{str(exc)[:200]}. Nobody has confirmed today's issue went out.", webhook)
        return 2
    if found:
        print(f"# outcome: published today - {detail}")
        return 0
    alarm(f"NO issue published today ({now:%Y-%m-%d}) and it is {now:%H:%M}. {detail}. "
          "Check that the publisher is installed and on the right revision, then "
          "re-run the build.", webhook)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--archive-url", required=True,
                    help="public archive endpoint returning a JSON list of posts")
    ap.add_argument("--timezone", default="America/New_York")
    ap.add_argument("--publish-at", default="09:00", help="local send slot, HH:MM")
    ap.add_argument("--grace-minutes", type=int, default=60)
    ap.add_argument("--publish-days", default="0,1,2,3,4",
                    help="comma-separated weekday numbers, Monday is 0")
    ap.add_argument("--date-field", default="post_date")
    ap.add_argument("--title-contains", default=None,
                    help="only count a post whose title contains this, so an "
                         "unrelated same-day post cannot mask a missed issue")
    ap.add_argument("--webhook", default=None, help="chat webhook for the alarm")
    ap.add_argument("--now", default=None, help="ISO timestamp, for testing")
    args = ap.parse_args(argv)

    tz = ZoneInfo(args.timezone)
    if args.now:
        parsed = datetime.fromisoformat(args.now)
        # A naive --now means "this local time", where local is --timezone. The
        # obvious .astimezone() would silently reinterpret it in the machine's
        # own zone, so the same command would answer differently on a laptop and
        # on a server, which is a poor property for the thing that decides
        # whether to wake somebody.
        now = (parsed.replace(tzinfo=tz) if parsed.tzinfo is None
               else parsed.astimezone(tz))
    else:
        now = datetime.now(tz)
    hh, mm = (int(p) for p in args.publish_at.split(":", 1))
    days = {int(d) for d in args.publish_days.split(",") if d.strip()}
    return check(archive_url=args.archive_url, tz=tz, publish_hour=hh,
                 publish_minute=mm, grace_minutes=args.grace_minutes,
                 publish_days=days, date_field=args.date_field, now=now,
                 webhook=args.webhook, title_contains=args.title_contains)


if __name__ == "__main__":
    sys.exit(main())
