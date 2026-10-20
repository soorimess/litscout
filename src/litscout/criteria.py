"""Review criteria: what belongs in the review. The LLM drafts them once from the topic and the seeds;
the user can edit runs/<run>/criteria.md, and every LLM judgment is made against that text."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from .llm import LLM
from .store import Store

log = logging.getLogger(__name__)

CRITERIA_SYSTEM = """You write the inclusion and exclusion criteria of a systematic literature review.

You get the review topic and the titles and abstracts of its seed papers. Return a JSON object
{"definition": "<one or two sentences: what a paper must be about to belong in the review>",
 "include": ["<criterion>", ...], "exclude": ["<criterion>", ...]}
with 3 to 6 inclusion and 3 to 6 exclusion criteria. Each criterion is one short, checkable sentence
about the paper's main contribution, judged from its title and abstract. Exclusion criteria name the
nearby work that is NOT part of the review (for example papers that only use the topic's data or tools
for another purpose, or broader areas that merely contain the topic)."""

JUDGE_SYSTEM = """You screen candidate papers for a systematic literature review.

{criteria}

Given one paper's title and abstract, judge it against these criteria. Return a JSON object
{{"score": <0-10>, "relevant": <true|false>, "reason": "<one sentence naming the criterion that decided>"}}.
Scale: 9-10 the paper's main contribution meets the inclusion criteria; 6-8 it meets them through a
substantial part of the work; 3-5 related, but it fails an inclusion criterion or meets an exclusion
criterion; 0-2 unrelated. Set "relevant" to true only when the score is 6 or higher."""


def render(topic: str, c: dict[str, Any]) -> str:
    inc = "\n".join(f"- {x}" for x in c.get("include", []))
    exc = "\n".join(f"- {x}" for x in c.get("exclude", []))
    return (f"# Review criteria\n\nTopic: {topic}\n\nDefinition: {c.get('definition', '').strip()}\n\n"
            f"## Include\n{inc}\n\n## Exclude\n{exc}\n")


def draft(topic: str, store: Store, llm: LLM) -> str:
    rows = store.q("SELECT p.title, p.abstract FROM seeds s JOIN papers p ON p.id = s.id")
    seeds = "\n\n".join(f"Title: {r['title']}\nAbstract: {(r['abstract'] or '')[:1200]}" for r in rows)
    out = llm.chat_json(CRITERIA_SYSTEM, f"Topic: {topic}\n\nSeed papers:\n\n{seeds}")
    return render(topic, out)


def digest(text: str) -> str:
    return hashlib.sha1(text.strip().encode()).hexdigest()[:12]


def load(store: Store, run_dir: Path) -> str:
    """The current criteria text. criteria.md in the run folder wins, so user edits take effect; judgments
    made under different criteria are archived, since their labels no longer apply."""
    path = run_dir / "criteria.md"
    text = path.read_text() if path.exists() else store.meta("criteria")
    if not text:
        raise SystemExit("Run has no review criteria yet; run the seed step first.")
    h = digest(text)
    old = store.meta("criteria_hash")
    if old and old != h:
        n = store.archive_judgments(old)
        store.event("criteria", "criteria changed", {"old": old, "new": h, "archived_judgments": n})
        log.info("review criteria changed (%s -> %s): %d earlier judgments archived", old, h, n)
    store.set_meta("criteria", text)
    store.set_meta("criteria_hash", h)
    if not path.exists():
        path.write_text(text)
    return text


def judge_system(criteria: str) -> str:
    return JUDGE_SYSTEM.format(criteria=criteria.strip())
