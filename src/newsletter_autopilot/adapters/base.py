"""The platform adapter contract.

Everything platform-specific lives behind this interface: routes, auth, payload
shapes, quirks. The pipeline never imports a concrete adapter, so adding
Ghost, Buttondown, Beehiiv, or an internal ESP is a new file plus a registry
entry, not a change to the code that decides whether to send.

TWO RULES EVERY ADAPTER MUST HONOUR. They are not style; they are why this
system has never sent twice.

 1. A 2xx IS NOT PROOF THE WRITE HAPPENED. Real newsletter APIs return 200 and
    silently do nothing (the tagging endpoint of the platform this was distilled
    from does exactly that). Every mutation must be confirmed by reading it back
    before the pipeline is told it succeeded.

 2. AN EMPTY LIST IS NOT PROOF OF ABSENCE. On a real platform this pattern was
    distilled from, the same listing endpoint returned zero rows at one page
    size and a full page at a smaller one. A reconcile that trusts an empty list
    will happily create a second post. An unreadable or suspicious response must
    raise `Ambiguous`, which parks the run, rather than being flattened into
    "nothing there".

And one that follows from them: an adapter NEVER exposes an immediate-publish
call. The only way an issue goes out is the platform firing a schedule that was
set here and confirmed by read-back. That removes a whole class of "just send it
now" scripts from ever being possible.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PublisherAdapter(Protocol):
    """What the pipeline needs from a publishing platform."""

    name: str

    # --- identity ---------------------------------------------------------

    def verify_identity(self) -> str:
        """Prove the credential works and return the publication it belongs to.

        Called before anything is created, so a dead credential costs zero
        writes. Raises NotAuthenticated with a plain-language fix.
        """

    # --- reconcile (reads that decide whether to write at all) ------------

    def scheduled_on(self, date: str) -> list[dict[str, Any]]:
        """Posts scheduled to fire on `date`, in the publication's own timezone.

        Must raise Ambiguous rather than return [] when the answer cannot be
        established. See rule 2.
        """

    def published_on(self, date: str) -> list[dict[str, Any]]:
        """Posts ALREADY PUBLISHED on `date`, read from the public surface.

        A post that already went out is not in the scheduled list, so this is a
        separate question, and the only one that catches "the issue is already
        out" during a same-day recovery.
        """

    # --- media ------------------------------------------------------------

    def upload_image(self, data: bytes, filename: str) -> str:
        """Rehost an image and return the platform-hosted URL."""

    # --- drafts -----------------------------------------------------------

    def create_draft(self, *, title: str, subtitle: str, body: dict[str, Any]) -> str:
        """Create a draft and return its id. Never publishes."""

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        """Read a draft back. Used to prove ownership and to seal content."""

    def update_draft(self, draft_id: str, **fields: Any) -> None:
        """Mutate a draft. The caller confirms by read-back; see rule 1."""

    def set_cover(self, draft_id: str, image_url: str) -> None:
        """Attach the cover image."""

    # --- the irreversible one --------------------------------------------

    def schedule(self, draft_id: str, trigger_at_utc: str, *,
                 audience: str = "everyone") -> None:
        """Set the send schedule. THIS IS THE CALL THAT EMAILS PEOPLE.

        Must raise Ambiguous (never PublisherError) on a timeout, a 5xx, a 429,
        or any other status where the server may have acted anyway.
        """

    def unschedule(self, draft_id: str) -> None:
        """Cancel a schedule that has not fired. For the cancel command only."""

    def public_render(self, draft_id: str) -> dict[str, Any]:
        """What a reader would actually see, from the public surface.

        Used by the post-send verification stage: "the API accepted it" and "a
        reader can read it" are two different claims.
        """


class BaseAdapter:
    """Small shared helpers. Subclassing is optional; the Protocol is the contract."""

    name = "base"

    # Statuses where the request MAY have taken effect server-side despite the
    # error, so a retry could double-act. These park the run instead.
    AMBIGUOUS_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

    def describe(self) -> str:
        return self.name
