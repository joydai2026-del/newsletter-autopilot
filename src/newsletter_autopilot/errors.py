"""The error vocabulary the whole pipeline is built around.

The important distinction is not "worked" versus "failed". It is:

    failed      the remote side provably did nothing. Retrying is safe.
    ambiguous   the remote side MAY have acted. Retrying could act twice.

An email to a subscriber list cannot be unsent, so the second case is never
treated as a retry signal. It parks the run and asks a human. Every module
here funnels timeouts, 5xx, and unreadable responses into `Ambiguous` rather
than letting them look like ordinary failures.
"""

from __future__ import annotations


class AutopilotError(RuntimeError):
    """Base for everything this package raises."""


class ConfigError(AutopilotError):
    """The run policy is missing or self-contradictory."""


class PublisherError(AutopilotError):
    """The platform said no, and provably did nothing."""


class Ambiguous(PublisherError):
    """The outcome is unknown. NEVER retry on this. Park and ask a human."""


class NotAuthenticated(PublisherError):
    """The credential is dead or expired. Needs a person, in plain language."""


class StateError(AutopilotError):
    """The durable run state was asked to do something incoherent."""


class LockHeld(StateError):
    """Another run holds the lock for this issue date."""


class Parked(StateError):
    """The run for this issue date is parked and needs a human decision."""


class LedgerError(AutopilotError):
    """A dedup ledger write was refused."""
