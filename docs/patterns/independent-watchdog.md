# Pattern: the watchdog lives outside the thing it watches

**The failure this exists for.** A daily newsletter missed an issue in complete
silence. The working directory had been left on a feature branch overnight. The
publisher's directory does not exist on that branch, so the morning's scheduled
run found no code, built nothing, published nothing, and raised nothing. Every
check that could have caught it lived inside the directory that had vanished.

A check that can disappear along with the thing it checks is not a check.

## Three rules

### 1. Deploy it separately, and let it import nothing

The watchdog here is a single standard-library file under `watchdog/`. It is
installed on its own schedule, it imports nothing from the pipeline package, and
it therefore still runs when the pipeline is missing, broken, half-installed, or
on the wrong revision.

This is enforced, not merely intended:
`test_watchdog_imports_nothing_from_the_pipeline` reads the source and fails if
the package name appears in it, and every other watchdog test loads the file by
path rather than importing it.

### 2. Check the outcome, not the machinery

On the day nothing shipped, every machinery-level signal looked healthy: the
scheduler fired, the exit code was 0, the log line was written. All of them were
true statements about a process. None of them was a statement about a reader's
inbox.

So the watchdog asks the one question that cannot be faked from inside: **does
the publication's own public archive contain a post published today?** It reads
the public surface, with no credential the pipeline holds, because a credential
shared with the pipeline is one more thing that can fail in the same way at the
same time.

### 3. A broken watchdog must never read as all clear

Three exit codes, not two:

| code | meaning |
|---|---|
| 0 | an issue is out, or today is not a publishing day, or the slot has not passed |
| 1 | nothing published, and the alarm has been raised |
| **2** | **the check itself could not run, and the alarm has been raised** |

Code 2 is the one people forget. An unreachable archive, HTML where JSON was
expected, or a shape the parser does not recognise all mean "nobody has confirmed
today's issue went out", which is a thing a human needs to hear. Collapsing that
into 0 gives you a monitor that goes quiet exactly when it has stopped working.

The alarm itself is best effort and never raises: if the notification channel is
down, the message is printed and the exit code still carries the verdict.

## One correctness detail worth stealing

Archives report timestamps in UTC. An issue's identity is a **local** date.
Comparing a raw UTC string against a local date agrees for a morning send and
disagrees for an evening one: a 21:05 local release is stored on the *next* UTC
day. In the original system that mismatch made an evening recovery invisible to
the very check meant to catch a double send, and it poisoned the next morning's
reconcile by looking like tomorrow's post.

Convert first, compare second. `test_an_evening_send_is_still_today_after_the_utc_conversion`
pins it.

## Running it

```bash
watchdog/outcome_check.py \
  --archive-url https://example.com/api/v1/archive?limit=12 \
  --timezone America/New_York --publish-at 09:00 --grace-minutes 60 \
  --webhook "$ALERT_WEBHOOK"
```

Schedule it after the send slot plus a grace period, on infrastructure that is
not the pipeline's. `--now` accepts an ISO timestamp so the schedule logic can be
exercised without waiting for a Tuesday.
