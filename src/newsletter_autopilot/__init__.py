"""newsletter-autopilot: a daily newsletter that publishes itself, at most once.

The public surface is small on purpose.
"""

from .config import Config, Policy
from .errors import (
    Ambiguous,
    AutopilotError,
    LedgerError,
    LockHeld,
    NotAuthenticated,
    Parked,
    PublisherError,
    StateError,
)
from .issue import Issue, Story, build_issue, load_stories, select_stories
from .ledger import Ledger, filter_unused, record_issue
from .locking import RunLock
from .pipeline import Pipeline, Result
from .state import RunState

__version__ = "0.1.0"

__all__ = [
    "Ambiguous", "AutopilotError", "Config", "Issue", "Ledger", "LedgerError",
    "LockHeld", "NotAuthenticated", "Parked", "Pipeline", "Policy", "PublisherError",
    "Result", "RunLock", "RunState", "StateError", "Story", "build_issue",
    "filter_unused", "load_stories", "record_issue", "select_stories", "__version__",
]
