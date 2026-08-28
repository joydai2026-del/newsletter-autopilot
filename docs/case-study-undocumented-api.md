# Case study: integrating against a publishing API that has no docs

This is a write-up of a method, not a route list. It describes how the
production system this repo was distilled from came to have a reliable
integration with a newsletter platform that publishes no write API, and, more
importantly, what had to be true before that integration was allowed near a real
subscriber list.

The concrete route table for that platform is **deliberately not reproduced
here**. It belongs to one publication's account, it changes without notice, and
a copy-paste list is the least useful part of the work. What transfers is the
method and the safety conditions.

## The problem

The platform offers a full-featured web dashboard and no documented API for
creating or scheduling a post. Publishing a daily issue therefore meant a person
opening a browser every morning, which is exactly the work that was being
automated away.

Browser automation was considered and rejected: a headless browser driving a
publish button is slower, far more fragile against markup changes, and gives you
no clean idempotency story. If the click times out, you cannot tell whether the
post went out.

## Recovering the surface

The web app is a client. It is already calling something, and everything it
calls is visible from the browser's own network panel and from the JavaScript
bundles the site ships to every visitor.

The sequence that worked:

1. **Watch the network panel while doing the task by hand.** Create a draft,
   attach an image, set a schedule, cancel it. Every step names a route, a
   method, and a payload shape.
2. **Read the shipped bundles for the routes you did not trigger.** The
   dashboard ships its whole client, including calls behind features you have
   not used. Fetching the bundle list and grepping the sources for the API
   prefix produced the routes that hand-clicking never reached. In the original
   work this meant reading roughly 150 bundles, and it came *after* about fifteen
   guessed URLs that all returned 404. Guessing the call that emails real people
   is not a method.
3. **Verify every route live, on disposable objects.** Each candidate was
   exercised against a real account using throwaway drafts that were deleted
   afterwards. A route that was inferred but not exercised was treated as
   unknown.
4. **Write down the quirks next to the code that depends on them**, because the
   quirks are the part that will break you.

The shape turned out to be an ordinary REST resource: create a draft, update it,
set a send time on it, delete that send time to cancel. Nothing exotic, and
nothing worth transcribing here. The interesting engineering is not the route
list, it is the two rules below.

## The two quirks that shaped the whole design

**A 2xx is not proof the write happened.** One endpoint on that platform returns
`200 OK` and silently does nothing. Once you have seen that, every mutation in
the pipeline has to be confirmed by reading it back, and "the call succeeded" is
demoted to "the call did not obviously fail". In this repo that rule lives in
`adapters/base.py` and is enforced by `Pipeline._seal_content`,
`_set_cover_confirmed`, and `_confirm_schedule`.

**An empty list is not proof of absence.** On the same platform, listing drafts
with `limit=50` returned zero results while `limit=5` returned five. A reconcile
step that trusts an empty list will conclude "nothing is scheduled for today" and
create a second issue. So the reconcile reads the surfaces the real dashboard
uses, and any response it cannot establish raises `Ambiguous`, which parks the
run. `MockPublisher(empty_list_lie=True)` reproduces this, and
`test_an_unreadable_reconcile_parks_rather_than_creating` pins it.

## The conditions that made it safe to ship

An undocumented integration is only as good as what happens when it is wrong.
Four conditions, all of them visible in this repo:

- **No immediate-publish call exists anywhere in the codebase.** The only way an
  issue goes out is the platform firing a schedule that was set by this code and
  confirmed by read-back. Even the same-day recovery path releases by scheduling
  a few minutes out. That removes "just send it now" as a reachable state, for
  humans and for agents.
- **Ambiguity parks; it never retries.** A timeout on the scheduling call is the
  one case where the server may already have acted, so it is the one case where a
  retry sends twice. See `docs/patterns/at-most-once-publishing.md`.
- **Everything platform-specific is behind one interface.** The pipeline imports
  `PublisherAdapter`, never a concrete client. When a recovered route changes,
  one file changes. When the platform later ships a real API, one file changes.
- **An independent watchdog checks the outcome, not the machinery.** A recovered
  API can start failing in ways your own logs call success. The watchdog reads
  the public archive and asks whether readers got an issue today. See
  `docs/patterns/independent-watchdog.md`.

## When not to do this

Reading a product's own public frontend to learn how it talks to its own backend
is ordinary client-side inspection, and the calls are made with the account
holder's own credential, for the account holder's own content. That is the case
this was: one person automating their own publication.

It is still worth being explicit about the limits. Do not do this to act on
accounts you do not own; check the terms you agreed to; keep request volume in
the same range a person clicking the dashboard would produce; and expect no
stability guarantee, which is why the adapter boundary and the watchdog are not
optional extras here. If the platform ships a supported API, move to it.
