# Pattern: lock asymmetry, or why stale-lock reclaiming is usually a bug

Most lock implementations treat a lock file like a mutex: if it looks abandoned,
take it. That default is wrong whenever the two failure modes cost different
amounts, and around an irreversible action they always do.

## The two mistakes are not symmetric

| mistake | what happens | recoverable? |
|---|---|---|
| **steal a lock whose holder is ALIVE** | two runs proceed. Neither one's reconcile sees the other's not-yet-scheduled draft, because a draft is not a schedule. Two issues are emailed. | **no** |
| **hold a lock whose holder is DEAD** | today's issue does not go out, and the operator is notified | yes, in minutes |

So the logic is not symmetric either. **Every uncertain answer resolves toward
"the holder is alive."** An unreadable lock file, a process probe that errors, an
unexpected `OSError` from the liveness check: all of them mean alive.

There is a name for the general shape. Grabbing work is irrecoverable if you are
wrong, so it demands proof. Concluding the holder died only causes a skip plus a
notification, so it can be conservative. Design the two directions separately.

## Age is not proof of death

The obvious implementation reclaims any lock older than N minutes. That is
precisely the irrecoverable mistake: a slow-but-alive run gets its lock stolen at
minute N+1. Slow runs are normal, and they are most likely on exactly the bad
days when you least want a second run.

Age is used here only as a last resort, for a lock that names no process anyone
can check.

## A pid is not an identity

Pids get recycled. A recycled pid makes an abandoned lock look alive forever,
which silently kills the newsletter with no notification: the failure mode this
system is least able to detect on its own.

pid **plus process start time** is a real identity. The operating system will not
hand out the same pair twice. So the lock token carries both:

```
pid=41207 at=2026-08-26T06:00:11-0400 start=Wed Aug 26 05:59:48 2026
```

and a holder is alive only if the pid exists **and** its start time matches what
the token recorded.

## The three checks, in order

1. `os.kill(pid, 0)` raising `ProcessLookupError` is the only positive proof of
   death. `PermissionError` means it exists and belongs to someone else. Any
   other `OSError` means alive, because it means "do not know".
2. If it exists, compare the recorded start time against the current one. A
   mismatch is a recycled pid: dead. A probe that fails or returns nothing is
   **not** a death certificate, so it reads as alive.
3. Only if the token names no parsable pid at all does age decide, and only past
   the configured staleness.

## Two details that are easy to get backwards

**Omit an unknown start time; never write an empty one.** A token with no
`start=` falls back to pid-plus-age, which errs toward holding the lock, and a
held lock notifies. An empty `start=` would never match a real one, so the lock
would be stolen from a live run. Absent and empty are opposite failure
directions.

**Do not delete the lock on exit unless it is still yours.** If this run was
wrongly declared stale and another run took the lock, deleting it on the way out
hands a third run a free pass while the second is still working. Compare the file
contents against the token you wrote.

## Where it is in this repo

`src/newsletter_autopilot/locking.py`, and every claim above is pinned in
`tests/test_locking.py`, including an ancient lock held by a live process, a
recycled pid, a failed probe counting as alive, and the exit-time check.
