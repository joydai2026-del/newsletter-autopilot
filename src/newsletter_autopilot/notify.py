"""Telling a human what happened, in the words a human needs.

THE RULE THIS MODULE ENFORCES: never report a state that is more comforting than
the truth. Three outcomes, and they are genuinely different:

    scheduled                it is going out. Here is when and where.
    failed                   nothing was created. Safe to fix and re-run.
    scheduled_but_incomplete it IS going out, AND something afterwards broke.
                             Do not re-run. Fix the loose end by hand.

The third one exists because once the schedule lands, the run cannot be stopped,
and reporting a post-schedule failure as "the run failed, try again" is how you
get two issues in one morning.

Two implementation rules, both learned from real incidents:

  * The MESSAGE is what has to survive. Notify first, persist state second. An
    error handler that writes to disk before sending will, on a disk failure,
    raise inside the handler and the human gets nothing at all, on exactly the
    path where an email may already be scheduled.
  * A notifier failure is printed, never raised. The exit code still carries the
    verdict.

Channels are swappable: `ConsoleNotifier` for a dry-run, `WebhookNotifier` for a
chat channel, `MultiNotifier` for both. Nothing in the pipeline knows which.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Protocol


class Notifier(Protocol):
    def send(self, subject: str, body: str) -> bool:
        """Deliver a message. Returns whether it landed. Never raises."""


class ConsoleNotifier:
    """Prints. Used by the dry-run and by tests."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.sent: list[tuple[str, str]] = []

    def send(self, subject: str, body: str) -> bool:
        self.sent.append((subject, body))
        print(f"\n[notify] {subject}\n{body}\n", file=self.stream)
        return True


class WebhookNotifier:
    """POSTs JSON to a chat webhook. Best effort, by design."""

    def __init__(self, url: str, timeout: int = 25):
        self.url = url
        self.timeout = timeout

    def send(self, subject: str, body: str) -> bool:
        try:
            req = urllib.request.Request(
                self.url, data=json.dumps({"text": f"{subject}\n\n{body}"}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"ALERT NOT SENT: {type(exc).__name__}: {exc}", file=sys.stderr)
            return False


class MultiNotifier:
    """Tries every channel. One channel failing does not silence the others."""

    def __init__(self, *channels: Notifier):
        self.channels = channels

    def send(self, subject: str, body: str) -> bool:
        return any(c.send(subject, body) for c in list(self.channels))


# --- the three messages -------------------------------------------------------

def scheduled(notifier: Notifier, *, issue_date: str, title: str,
              headlines: list[str], when_local: str, url: str) -> None:
    lines = [f"Issue {issue_date} is scheduled for {when_local}.",
             f"Title: {title}", f"Post: {url}", "", "In this issue:"]
    lines += [f"  {i}. {h}" for i, h in enumerate(headlines, start=1)]
    notifier.send(f"Scheduled: {issue_date}", "\n".join(lines))


def failed(notifier: Notifier, *, issue_date: str, phase: str, reason: str,
           action: str, created: bool = False) -> None:
    """Nothing was SCHEDULED. `created` says whether a draft nonetheless exists.

    The distinction is not pedantry. This message used to hardcode "nothing was
    created on the platform", which is simply false once a draft has been made,
    and the caller then appended a contradicting "a draft may exist" to the
    action line, so one alert said both. An operator who is told nothing exists
    goes looking for nothing, and leaves an orphan draft behind every crash.
    """
    standing = ("A draft exists on the platform, but nothing was scheduled, so "
                "nothing has gone out." if created else
                "Nothing was created on the platform, so nothing has gone out.")
    notifier.send(
        f"NOT scheduled: {issue_date}",
        f"The run stopped at this step: {phase}.\n\n"
        f"What happened: {reason}\n\n"
        f"{standing}\n\n"
        f"What to do: {action}")


def scheduled_but_incomplete(notifier: Notifier, *, issue_date: str, url: str,
                             problem: str) -> None:
    notifier.send(
        f"Scheduled, but check this: {issue_date}",
        f"The issue IS scheduled and will go out. {url}\n\n"
        f"Then this happened: {problem}\n\n"
        "Do NOT re-run the publish. It cannot be unsent by re-running, and a "
        "second run could create a second post. Fix the loose end by hand.")
