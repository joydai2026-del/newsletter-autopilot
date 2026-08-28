"""Stories in, an issue document out.

The ingest and drafting stages. Both are deliberately boring: the interesting
safety work happens later, and the way to keep it interesting is to make sure a
malformed input never reaches the platform at all.

Every validation here runs BEFORE the first remote byte. An earlier version of
the pipeline this was distilled from read the cover image halfway through the
run, after five images had already been rehosted, so "a broken input reaches the
platform zero times" was not actually true for a missing cover. Local inputs are
now all validated up front.

The body document is a small, platform-neutral block format. Adapters translate
it into whatever their platform wants (ProseMirror, HTML, Lexical, Markdown).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .errors import AutopilotError


class DraftRejected(AutopilotError):
    """The draft is not fit to send. Nothing remote has been contacted."""


@dataclass(frozen=True)
class Story:
    """One queued source item."""

    id: str
    url: str
    headline: str
    summary: str = ""
    comment: str = ""
    image: str = ""
    used: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Story:
        missing = [k for k in ("id", "url", "headline") if not str(raw.get(k) or "").strip()]
        if missing:
            raise DraftRejected(f"story is missing {', '.join(missing)}: {str(raw)[:120]}")
        known = {f for f in cls.__dataclass_fields__}          # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class Issue:
    """A finished, validated issue, ready for the publish stages."""

    date: str
    title: str
    subtitle: str
    doc: dict[str, Any]
    stories: list[Story] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    segment: str = ""

    @property
    def source_ids(self) -> list[str]:
        return [s.id for s in self.stories]

    @property
    def source_urls(self) -> list[str]:
        return [s.url for s in self.stories]

    def assets(self) -> dict[str, list[str]]:
        """What the dedup ledgers should record once this issue is scheduled."""
        out: dict[str, list[str]] = {"stories": self.source_urls,
                                     "images": list(self.images)}
        if self.segment:
            out["segments"] = [self.segment]
        return out


# --- ingest -------------------------------------------------------------------

def load_stories(path: Path | str) -> list[Story]:
    """Read the story queue. A real deployment swaps this for its own source
    (a database, a CMS table, an RSS sweep); nothing downstream cares."""
    raw = json.loads(Path(path).read_text(encoding="utf8"))
    if isinstance(raw, dict):
        raw = raw.get("stories", [])
    if not isinstance(raw, list):
        raise DraftRejected("the story queue must be a list, or an object with a "
                            "'stories' list")
    return [Story.from_dict(s) for s in raw]


def select_stories(stories: list[Story], cfg: Config) -> list[Story]:
    """Pick this issue's stories, oldest first, skipping anything already used.

    Refuses to run short. Publishing four stories in a five-story format is a
    visible defect, and the queue being thin is something a person can fix in a
    minute if they are told; it is not something to paper over.
    """
    want = cfg.policy.stories_per_issue
    fresh = [s for s in stories if not s.used]
    seen: set[str] = set()
    picked: list[Story] = []
    for s in fresh:
        if s.url in seen:
            continue
        seen.add(s.url)
        picked.append(s)
        if len(picked) == want:
            break
    if len(picked) < want:
        raise DraftRejected(
            f"only {len(picked)} unused stories are queued, and an issue needs "
            f"{want}. Add stories to the queue, then re-run.")
    return picked


# --- draft --------------------------------------------------------------------

_EM_DASH = re.compile(r"[—–]")


def build_issue(date: str, stories: list[Story], cfg: Config, *,
                title: str | None = None, subtitle: str | None = None,
                segment: str = "") -> Issue:
    """Assemble the issue document from the selected stories."""
    blocks: list[dict[str, Any]] = []
    images: list[str] = []
    for i, s in enumerate(stories, start=1):
        blocks.append({"type": "heading", "level": 2, "text": f"{i}. {s.headline}"})
        if s.image:
            blocks.append({"type": "image", "src": s.image, "alt": s.headline})
            images.append(s.image)
        if s.summary:
            blocks.append({"type": "paragraph", "text": s.summary})
        if s.comment:
            blocks.append({"type": "quote", "text": s.comment})
        blocks.append({"type": "link", "href": s.url, "text": "Read the source"})

    issue = Issue(
        date=date,
        title=title or f"{cfg.publication_name}, {date}",
        subtitle=subtitle or f"{len(stories)} things worth knowing today",
        doc={"type": "doc", "content": blocks},
        stories=list(stories),
        images=images,
        segment=segment,
    )
    validate_issue(issue, cfg)
    return issue


def validate_issue(issue: Issue, cfg: Config) -> None:
    """Every check that can be made without contacting anything.

    A failure here costs zero remote calls, which is the entire point of doing it
    before the lock is taken rather than mid-run.
    """
    if not issue.title.strip():
        raise DraftRejected("the issue has no title")
    if len(issue.stories) != cfg.policy.stories_per_issue:
        raise DraftRejected(
            f"expected {cfg.policy.stories_per_issue} stories, found {len(issue.stories)}")
    if len(issue.images) != cfg.policy.images_per_issue:
        raise DraftRejected(
            f"expected {cfg.policy.images_per_issue} images, found {len(issue.images)}")
    if cfg.policy.forbid_em_dashes:
        offenders = [t for t in collect_text(issue.doc) if _EM_DASH.search(t)]
        if offenders:
            raise DraftRejected(
                "the draft contains an em dash, which this publication's style "
                f"forbids: {offenders[0][:80]!r}")


def collect_text(doc: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for block in doc.get("content", []):
        text = block.get("text")
        if isinstance(text, str):
            out.append(text)
    return out


def headlines(issue: Issue) -> list[str]:
    return [s.headline for s in issue.stories]


def issue_to_json(issue: Issue) -> str:
    payload = asdict(issue)
    payload["stories"] = [asdict(s) for s in issue.stories]
    return json.dumps(payload, indent=2, sort_keys=True)
