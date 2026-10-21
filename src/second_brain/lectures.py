"""Lecture summaries: summarize each transcript section, then combine the section summaries
(docs/superpowers/specs/2026-10-21-lecture-mode-design.md §3). Hermes output is untrusted text;
it only ever lands in the lecture's own summary note."""

from __future__ import annotations
import time
from . import router
from .router import RouterQueue
from .vault import _clock, spoken_windows

SECTION_PROMPT = """You summarize one section of a lecture transcript for the listener's study notes.

Return ONLY valid JSON. Do not use tools. Do not read or write files. Do not browse.
Use only what the transcript says; do not add outside facts. Speaker labels may be anonymous.

Use exactly this schema:
{"summary": "3-6 sentences: the concepts, definitions, examples and announcements in this section"}
"""

COMBINE_PROMPT = """You combine the section summaries of one lecture into study notes.

Return ONLY valid JSON. Do not use tools. Do not read or write files. Do not browse.
Use only the supplied section summaries; do not add outside facts.

Use exactly this schema:
{"summary": "Markdown: a one-paragraph overview, then a bulleted list of the key points, then any assignments, deadlines or announcements (leave that part out when there are none)"}
"""


def _summary(reply: dict) -> str:
    text = reply.get("summary")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Hermes returned no summary")
    return text.strip()


def summarize(cfg: dict, manifest: dict) -> dict:
    """Map every spoken section, then reduce. Any Hermes failure raises, so the queue retries the
    whole summary; nothing partial is ever published."""
    sections = []
    for w in spoken_windows(manifest):
        user = f"SECTION: {_clock(w['start'])}–{_clock(w['end'])}\n\nTRANSCRIPT:\n{w['final_transcript'].strip()}\n"
        sections.append({"id": w["id"], "start": w["start"], "end": w["end"],
                         "summary": _summary(router.chat(cfg, SECTION_PROMPT, user))})
    if not sections:
        raise ValueError("Lecture has no transcript to summarize")
    user = "SECTION SUMMARIES:\n\n" + "\n\n".join(f"[{_clock(s['start'])}] {s['summary']}" for s in sections) + "\n"
    return {"version": 1, "summary": _summary(router.chat(cfg, COMBINE_PROMPT, user)), "sections": sections}


class SummaryQueue(RouterQueue):
    """Lecture jobs waiting for a summary: RouterQueue's table, backoff and retry in its own
    database. `text` stays empty; the transcript is read from the job directory."""

    def enqueue(self, job: str, observed_at: str) -> int:
        now = time.time()
        with self.connect() as db:
            return db.execute(
                "INSERT OR IGNORE INTO routes(id,job,text,observed_at,created,updated) VALUES(?,?,?,?,?,?)",
                (job, job, "", observed_at, now, now)).rowcount
