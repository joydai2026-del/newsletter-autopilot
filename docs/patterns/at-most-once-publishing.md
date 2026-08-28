# Pattern: at-most-once publishing

**The property.** An issue is emailed to the list at most once, ever, under
crashes, concurrent runs, timeouts, hand-edited state, and operator retries. Not
"usually once". At most once.

**Why at-most-once and not exactly-once.** Exactly-once delivery across a network
boundary you do not control is not available. You get to choose which way to be
wrong. Sending twice is irrecoverable and lands in front of the whole list;
sending zero times is recoverable in minutes *provided somebody is told*. So the
system is built to fail toward zero, and a separate, independent watchdog exists
solely to make sure the zero case is never silent. The two halves only work
together: at-most-once without a watchdog is just an unreliable newsletter.

## The mechanism

```
one exclusive lock per ISSUE DATE (not per invocation)
  |
  +- state says we are past 'scheduled'?  -> no-op, exit 0
  +- state is parked?                     -> refuse; a human decides
  +- inside the cutoff window?            -> park, notify, contact nothing
  +- reconcile: anything scheduled OR published for this date? -> park
  |
  +- write the phase BEFORE the call
  +- make the call
  +- confirm by READING IT BACK
  +- write the phase AFTER
```

Five rules make it hold.

### 1. The durable record is written before the call, not after

A crash between "write" and "call" leaves a state that claims an action was
attempted which never landed. That is pessimistic, and pessimistic is recoverable
by a human looking once. The reverse order leaves a state that says nothing
happened when something did, which is how you send twice.

`RunState.advance("schedule_pending")` fires before `adapter.schedule(...)`.

### 2. Ambiguous is not a failure, and it is never a retry signal

Three outcomes, not two:

| outcome | the server | correct response |
|---|---|---|
| success | acted | continue |
| failure | provably did not act | safe to retry |
| **ambiguous** | **may have acted** | **park. Ask a human.** |

Timeouts, 5xx, 429, 409, and any response the client cannot parse are ambiguous.
`Ambiguous` is a distinct exception type for exactly this reason: a shared
`except PublisherError: retry` would send twice on a slow network.

### 3. A success response is not proof of a write

Real APIs return 200 and do nothing. Every mutation is confirmed by reading it
back before the pipeline believes it. The confirmation immediately before the
schedule call is the important one: a schedule read-back proves only the trigger
time and the audience, so the content is sealed separately. An un-applied body
update would otherwise schedule yesterday's content to a real list.

### 4. State is a fast path; reconcile is the safety property

Local state can be deleted, corrupted, or hand-edited by an operator who wants
the pipeline to "try again". So before creating anything, ask the platform two
separate questions:

- what is **scheduled** for this date?
- what is already **published** on this date?

Both, because a post that already went out is not in the scheduled list, and the
same-day recovery runs in exactly that state. If either question cannot be
answered, that is `Ambiguous`, and the run parks rather than creating a post on
the strength of a list it could not read.

`test_a_fresh_state_dir_still_cannot_double_send` deletes the entire state
directory and proves the platform-side reconcile still refuses.

### 5. Give the refusal a supported next move

This is the rule people skip, and it is the one that caused the worst incident in
the original system. The pipeline refused to publish after its cutoff and offered
no alternative. Three separate times an operator wanted the issue out anyway,
found no supported path, and hand-wrote a script that called the platform's
immediate-publish endpoint. One of those scripts unscheduled a correct future
send and mailed the issue the night before.

A refusal with no supported next move is what produces the bypass. So the next
move lives inside the audited code as `recover`, with its own guards:

- it still releases **by scheduling**, minutes out, never by publishing
- it runs **at most once successfully** per issue date, enforced by a marker in
  the durable audit trail, not by an operator's memory
- the marker's two halves are separate on purpose. `recovery_attempted` is
  written as the last statement before the first mutating call, so a failure
  during the reads above it costs nothing and the date stays retryable. The
  absence of a matching `recovery_outcome` is what tells a later run "something
  may have landed, reconcile before you create anything"
- attempts are bounded. After the ceiling, a further retry is not the answer and
  the command says so

Every one of these is pinned by a test in `tests/test_at_most_once.py`.

## What it looks like when it works

The visible behaviour is boring: re-running a completed publish prints "already
scheduled" and exits 0. The interesting behaviour is what happens when you try
to break it, which is what the test file is.
