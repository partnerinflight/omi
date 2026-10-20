"""Knowledge router: turn published conversations into decisions, facts, tasks and ideas.

Ported from the standalone C:\\second-brain-router (v4.1) so the service runs it for every
published window. Hermes extracts candidates, collapses duplicates inside the batch, then
reconciles them against everything the router already wrote (new / duplicate / refinement /
conflict). Items are appended under "## Router Inbox" in the vault; curated prose is never
rewritten, and every routed source gets a ledger record under System/Router/Ledger.
"""

from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from .io import atomic_write, write_json
from .clarifications import NOTE_LOCK
from .router_markers import MARKER, marker as event_marker

MANAGED_HEADER = "## Router Inbox"
ENTITY_FOLDERS = {"person": "People", "project": "Projects", "topic": "Topics"}
LAYOUT = ["People", "Projects", "Topics", "Decisions", "Ideas", "Daily",
          "System/Router/Ledger", "System/Router/Reconciliation"]

EXTRACT_PROMPT = """You are a strict information extraction component for a personal knowledge router.

Return ONLY valid JSON. Do not use tools. Do not read or write files. Do not browse.
Do not infer private facts that are not explicitly supported by the source.
Ignore jokes, obvious hypotheticals, speculation, and filler unless they are themselves important.
Prefer fewer high-confidence items over exhaustive extraction.

Use exactly this schema:
{
  "facts": [
    {
      "entity_type": "person" | "project" | "topic",
      "entity_name": "canonical short name",
      "fact": "one atomic durable fact",
      "confidence": 0.0-1.0,
      "observed_at": "ISO timestamp or null"
    }
  ],
  "decisions": [
    {"text": "decision and rationale if present", "confidence": 0.0-1.0, "observed_at": "ISO timestamp or null"}
  ],
  "tasks": [
    {"text": "commitment/action", "owner": "name or null", "due": "date/time or null", "confidence": 0.0-1.0}
  ],
  "ideas": [
    {"text": "idea worth retaining", "confidence": 0.0-1.0}
  ],
  "daily_summary": ["important chronological event"],
  "clarifications": [{"text": "exact text of an extracted item", "question": "specific missing-context question"}]
}

Rules:
- Apply the usefulness threshold to EVERY category, including daily_summary and tasks.
  A retained conversation is not permission to capture every statement in it.
  Keep only information that changes a future action, records a consequential outcome,
  or supplies durable context worth retrieving weeks later. Empty arrays are preferred
  over a diary of ordinary conversation. Naming the speaker does not make an item useful.
- Omit routine studying, homework, piano practice, chores, temporary tidying, casual
  complaints about classes, shopping wishes, and vague intentions to make progress.
  "Speaker 1 had substantial studying to do and needed to practice piano" is noise;
  it stays omitted even if Speaker 1 is identified. Do not request clarification for it.
  By contrast, an adopted recurring lesson schedule or a concrete release test plan
  with deliverables can matter. Judge the consequences, not isolated keywords.
- Tasks require an adopted, specific, meaningful commitment, not "I need to" chatter.
  daily_summary is for consequential events/outcomes only, not summaries of what was
  discussed, reminders of ordinary chores, or duplicate copies of another category.
  Do not rescue rejected content by moving it into facts, ideas, or daily_summary.
- Decisions must be explicit real-world choices with lasting consequences for the owner,
  their projects, relationships, or meaningful commitments. A choice is not useful merely
  because someone accepted, chose, agreed, or decided something.
- Exclude routine shopping/bargaining (for example accepting 35 for two shoes), incidental
  one-off logistics, fictional characters' choices, read-aloud stories, games, and media.
  Never treat dialogue from these sources as the owner's personal knowledge.
- Suggestions ("the team should"), preferences, willingness, and general advice are not
  decisions unless the transcript establishes an actual adopted choice. Omit decisions
  without enough context to explain what changed and why it matters later, unless
  it is meaningful and a specific question can recover the missing context.
- Make each item understandable on its own: include the people/team, meeting or
  project involved when supported anywhere in the source. A named beneficiary
  is not necessarily the other participant ("so Eugene can attend" does not
  identify whom he meets). Never invent a name or infer who a pronoun refers to.
- For useful but incomplete items, add a clarification with the EXACT extracted
  text and a concise question. Example: a recurring Wednesday 11:00 meeting with
  no named counterpart needs "Who is this recurring meeting with, and what is it for?"
  Flag unresolved people, anonymous Speaker labels, or missing meeting/project
  context needed to understand or act on the item. No question is needed when
  the source identifies a sufficient team or role. Do not ask about immaterial
  details or manufacture useful items just to ask questions. Empty lists are valid.
- Keep meaningful project/release/pricing choices, adopted team policies, recurring schedule
  changes, and consequential personal commitments. Empty decisions arrays are valid.
- A fact must be atomic.
- A statement MAY legitimately produce both a durable entity fact and a decision when both are useful.
  Example: "We decided Whisper runs on the Threadripper" can produce:
  (a) a project fact describing the resulting architecture, and
  (b) a decision recording that the architecture was chosen.
- Do not create redundant copies within the same category.
- Do not turn uncertain language into certainty.
- Use the transcript metadata timestamp when the source does not state a more precise time.
- Entity names should be stable and human-readable.
"""

COLLAPSE_PROMPT = """You collapse semantic duplicates inside ONE extraction batch for a personal knowledge router.

The candidates may be facts or decisions. Two candidates are duplicates if they express substantially the same durable meaning, even if one is labeled fact and the other decision.

Return ONLY:
{
  "items": [
    {
      "index": 0,
      "action": "keep" | "duplicate",
      "duplicate_of": 0 | null,
      "reason": "brief explanation"
    }
  ]
}

Rules:
- Return exactly one item for every candidate index.
- The first/most useful representation of a meaning should be kept.
- A duplicate must point to an earlier kept candidate.
- When a fact and a decision encode the same meaning, prefer the decision if the source explicitly describes a deliberate choice ("decided", "chose", "will use", "agreed", etc.).
- Otherwise prefer the more specific, durable formulation.
- Do not collapse items merely because they are related.
- Do not infer beyond supplied text.
"""

GLOBAL_PROMPT = """You reconcile candidate durable knowledge against ALL existing durable knowledge in a personal knowledge base.

Existing items can be facts or decisions. Candidate items can also be facts or decisions.
Classification is semantic and CROSS-CATEGORY.

For every candidate classify as:
- new: meaning is not already represented.
- duplicate: same durable meaning is already represented, even if the existing item is a different category.
- refinement: adds meaningful specificity to an existing item without contradicting it.
- conflict: materially contradicts an existing item or indicates the old item may no longer be true.

Return ONLY:
{
  "items": [
    {
      "index": 0,
      "action": "new" | "duplicate" | "refinement" | "conflict",
      "matched_index": 0 | null,
      "reason": "brief explanation"
    }
  ]
}

Rules:
- Return exactly one item per candidate.
- `matched_index` refers to the index in existing_items; null only for new.
- Treat cross-category equivalence as duplicate. Example:
  existing decision: "Run Whisper on the Threadripper"
  candidate project fact: "Whisper runs on the Threadripper"
  => duplicate.
- Prefer duplicate over refinement when wording/rationale changes but durable meaning does not.
- Use refinement only when the candidate contributes materially new detail.
- Do not infer beyond supplied text.
"""


# --- Hermes -----------------------------------------------------------------------------------

def _json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Hermes returned no JSON object")
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("Hermes returned JSON that is not an object")
    return value


def chat(cfg: dict, system: str, user: str) -> dict:
    from .adaptive.pipeline import hermes_api_key

    headers = {"Content-Type": "application/json"}
    if key := hermes_api_key(cfg):
        headers["Authorization"] = f"Bearer {key}"
    body = {"model": cfg.get("hermes_model", "default"), "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    request = urllib.request.Request(cfg["hermes_url"], data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=cfg.get("router_timeout_seconds", 180)) as response:
        reply = json.loads(response.read().decode("utf-8"))
    return _json_object(reply["choices"][0]["message"]["content"])


# --- Validation (Hermes output is untrusted) ---------------------------------------------------

def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _confidence(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0.0 <= value <= 1.0 else None


def _items(obj: dict, key: str) -> list[dict]:
    value = obj.get(key, [])
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def parse_extraction(obj: dict) -> dict:
    """Keep only well-formed items; one malformed item does not discard the rest."""
    result = {"facts": [], "decisions": [], "tasks": [], "ideas": [], "daily_summary": [], "clarifications": []}
    for x in _items(obj, "facts"):
        name, fact, conf = _text(x.get("entity_name")), _text(x.get("fact")), _confidence(x.get("confidence"))
        if x.get("entity_type") in ENTITY_FOLDERS and name and fact and conf is not None:
            result["facts"].append(dict(entity_type=x["entity_type"], entity_name=name, fact=fact,
                                        confidence=conf, observed_at=_text(x.get("observed_at"))))
    for x in _items(obj, "decisions"):
        text, conf = _text(x.get("text")), _confidence(x.get("confidence"))
        if text and conf is not None:
            result["decisions"].append(dict(text=text, confidence=conf, observed_at=_text(x.get("observed_at"))))
    for x in _items(obj, "tasks"):
        text, conf = _text(x.get("text")), _confidence(x.get("confidence"))
        if text and conf is not None:
            result["tasks"].append(dict(text=text, owner=_text(x.get("owner")), due=_text(x.get("due")), confidence=conf))
    for x in _items(obj, "ideas"):
        text, conf = _text(x.get("text")), _confidence(x.get("confidence"))
        if text and conf is not None:
            result["ideas"].append(dict(text=text, confidence=conf))
    daily = obj.get("daily_summary", [])
    result["daily_summary"] = [t for t in (_text(x) for x in daily) if t] if isinstance(daily, list) else []
    for x in _items(obj, "clarifications")[:100]:
        text, question = _text(x.get('text')), _text(x.get('question'))
        if text and question and len(question) <= 500:
            result['clarifications'].append(dict(text=text, question=question))
    return result


def normalize_collapse(raw: dict, count: int) -> list[dict]:
    by_index = {x.get("index"): x for x in _items(raw, "items") if isinstance(x.get("index"), int)}
    kept, out = set(), []
    for i in range(count):
        item = by_index.get(i)
        action, target = (item.get("action"), item.get("duplicate_of")) if item else (None, None)
        if action == "duplicate" and isinstance(target, int) and target < i and target in kept:
            out.append(dict(index=i, action="duplicate", duplicate_of=target, reason=str(item.get("reason", ""))))
            continue
        reason = str(item.get("reason", "")) if item and action == "keep" else (
            "Collapser omitted or invalid; defaulted conservatively to keep.")
        kept.add(i)
        out.append(dict(index=i, action="keep", duplicate_of=None, reason=reason))
    return out


def normalize_global(raw: dict, count: int, existing: int) -> list[dict]:
    by_index = {x.get("index"): x for x in _items(raw, "items") if isinstance(x.get("index"), int)}
    out = []
    for i in range(count):
        item = by_index.get(i) or {}
        action, match = item.get("action"), item.get("matched_index")
        valid_match = isinstance(match, int) and 0 <= match < existing
        if action in ("duplicate", "refinement", "conflict") and valid_match:
            out.append(dict(index=i, action=action, matched_index=match, reason=str(item.get("reason", ""))))
        else:
            reason = str(item.get("reason", "")) if action == "new" else "Missing or invalid match; defaulted to new."
            out.append(dict(index=i, action="new", matched_index=None, reason=reason))
    return out


# --- Vault ------------------------------------------------------------------------------------

def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).strip().rstrip(".")
    return name[:120] or "Unknown"


def source_hash(source_id: str, text: str) -> str:
    return hashlib.sha256((source_id + "\n" + text).encode("utf-8")).hexdigest()


def event_id(shash: str, kind: str, key: str, text: str) -> str:
    payload = "\n".join([shash, kind, key.casefold().strip(), text.casefold().strip()])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def ledger_path(vault: Path, shash: str) -> Path:
    return vault / "System" / "Router" / "Ledger" / f"{shash}.json"


def read_managed_items(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if MANAGED_HEADER not in text:
        return []
    items = []
    for line in text.split(MANAGED_HEADER, 1)[1].splitlines():
        if line.startswith("## "):
            break
        if line.startswith("- "):
            body = line[2:].strip()
            if " — " in body:
                body = body.split(" — ", 1)[1]
            body = MARKER.sub("", body).rstrip()
            # Compare meaning, not the reconciliation annotation.
            body = re.sub(r"^\[(?:REFINEMENT|CONFLICT)(?: of|S with)?:.*?\]\s*", "", body)
            if body:
                items.append(body)
    return items


def existing_knowledge(vault: Path, decision_files: int = 90) -> list[dict]:
    items = []
    for entity_type, folder in ENTITY_FOLDERS.items():
        for path in sorted((vault / folder).glob("*.md")):
            if path.name != "_Tasks.md":
                items += [dict(kind="fact", scope=f"{entity_type}:{path.stem}", text=t)
                          for t in read_managed_items(path)]
    for path in sorted((vault / "Decisions").glob("*.md"), reverse=True)[:decision_files]:
        items += [dict(kind="decision", scope="global decisions", text=t) for t in read_managed_items(path)]
    return items


def append_item(path: Path, bullet: str, eid: str) -> bool:
    with NOTE_LOCK:
        return _append_item(path, bullet, eid)


def _append_item(path: Path, bullet: str, eid: str) -> bool:
    bullet = re.sub(r"\s+", " ", bullet.replace("&#x20;", " ").replace("\\\n", " ")).strip()
    marker = event_marker(eid)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if eid in MARKER.findall(existing):
        return False
    if not existing and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.stem):
        existing = f"# {path.stem}\n\n"
    if MANAGED_HEADER not in existing:
        existing += ("" if existing.endswith("\n") else "\n") + f"\n{MANAGED_HEADER}\n"
    existing += ("" if existing.endswith("\n") else "\n") + f"- {bullet} {marker}\n"
    atomic_write(path, existing.encode("utf-8"))
    return True


def _annotation(action: str, matched: dict | None) -> str:
    if action == "refinement":
        return f"[REFINEMENT of {matched['kind']}: {matched['text']}] " if matched else "[REFINEMENT] "
    if action == "conflict":
        return f"[CONFLICTS with {matched['kind']}: {matched['text']}] " if matched else "[CONFLICT] "
    return ""


# --- One source -------------------------------------------------------------------------------

def route(cfg: dict, vault: Path, source_id: str, text: str, observed_at: str, clarifications=None) -> dict:
    """Route one source. Idempotent: a source with a ledger record is skipped."""
    shash = source_hash(source_id, text)
    if ledger_path(vault, shash).exists():
        return {"skipped": "already routed"}
    for folder in LAYOUT:
        (vault / folder).mkdir(parents=True, exist_ok=True)

    user = f"SOURCE_ID: {source_id}\nDEFAULT_OBSERVED_AT: {observed_at or 'unknown'}\n\nSOURCE:\n{text}\n"
    extraction = parse_extraction(chat(cfg, EXTRACT_PROMPT, user))
    def publish(path, bullet, eid, content):
        questions = list(dict.fromkeys(x['question'] for x in extraction['clarifications'] if x['text'] == content))
        if re.search(r'\b(?:Speaker\s+\d+|unknown speaker|unidentified person)\b', content, re.I) and not questions:
            questions = ['Who is the unidentified person mentioned here?']
        # Persist the review first so crash replay cannot leave an untracked item.
        if questions and clarifications is not None:
            clean = re.sub(r'\s+', ' ', bullet.replace('&#x20;', ' ').replace('\\\n', ' ')).strip()
            clarifications.add(eid, path, clean, questions, text)
        return append_item(path, bullet, eid)
    payloads = [("fact", f) for f in extraction["facts"]] + [("decision", d) for d in extraction["decisions"]]
    candidates = [
        dict(index=i, kind=kind,
             scope=f"{item['entity_type']}:{item['entity_name']}" if kind == "fact" else "global decisions",
             text=item["fact"] if kind == "fact" else item["text"])
        for i, (kind, item) in enumerate(payloads)
    ]

    # One candidate cannot duplicate another; skip that Hermes call.
    if len(candidates) > 1:
        raw = chat(cfg, COLLAPSE_PROMPT, json.dumps({"candidates": candidates}, ensure_ascii=False, indent=2))
    else:
        raw = {"items": [dict(index=0, action="keep", reason="single candidate")] if candidates else []}
    collapse = normalize_collapse(raw, len(candidates))
    survivors = [c["index"] for c in collapse if c["action"] == "keep"]
    surviving = [dict(candidates[orig], index=i) for i, orig in enumerate(survivors)]

    existing = existing_knowledge(vault)
    if not surviving:
        verdicts = []
    elif not existing:
        verdicts = [dict(index=i, action="new", matched_index=None, reason="No existing durable knowledge.")
                    for i in range(len(surviving))]
    else:
        raw = chat(cfg, GLOBAL_PROMPT, json.dumps(
            {"existing_items": [dict(index=i, **x) for i, x in enumerate(existing)], "candidates": surviving},
            ensure_ascii=False, indent=2))
        verdicts = normalize_global(raw, len(surviving), len(existing))

    counts = {kind: {"new": 0, "duplicate": 0, "refinement": 0, "conflict": 0, "written": 0,
                     "batch_duplicate": sum(1 for c in collapse if c["action"] == "duplicate"
                                            and candidates[c["index"]]["kind"] == kind[:-1])}
              for kind in ("facts", "decisions")}
    for verdict in verdicts:
        kind, item = payloads[survivors[verdict["index"]]]
        bucket = counts[kind + "s"]
        bucket[verdict["action"]] += 1
        if verdict["action"] == "duplicate":
            continue
        matched = existing[verdict["matched_index"]] if verdict["matched_index"] is not None else None
        prefix = _annotation(verdict["action"], matched)
        when = item.get("observed_at") or observed_at
        if kind == "fact":
            path = vault / ENTITY_FOLDERS[item["entity_type"]] / f"{safe_name(item['entity_name'])}.md"
            eid = event_id(shash, "fact", f"{item['entity_type']}:{item['entity_name']}", item["fact"])
            body = item["fact"]
        else:
            path = vault / "Decisions" / f"{safe_name(when[:10])}.md"
            eid = event_id(shash, "decision", when[:10], item["text"])
            body = item["text"]
        bullet = f"{when} — {prefix}{body}"
        bucket["written"] += int(publish(path, bullet, eid, body))

    day = safe_name(observed_at[:10])
    counts["tasks"] = 0
    for t in extraction["tasks"]:
        meta = []
        if t["owner"]:
            meta.append(f"owner: {t['owner']}")
        if t["due"]:
            meta.append(f"due: {t['due']}")
        suffix = f" ({'; '.join(meta)})" if meta else ""
        bullet = f"{t['text']}{suffix}"
        counts["tasks"] += publish(vault / "Projects" / "_Tasks.md", bullet,
                                   event_id(shash, "task", t["owner"] or "", t["text"]), t['text'])
    counts["ideas"] = sum(publish(vault / "Ideas" / f"{day}.md",
                                      i['text'],
                                      event_id(shash, "idea", day, i["text"]), i['text']) for i in extraction["ideas"])
    counts["daily"] = sum(publish(vault / "Daily" / f"{day}.md", t,
                                      event_id(shash, "daily", day, t), t) for t in extraction["daily_summary"])

    reconciliation = {"batch_collapse": {"candidates": candidates, "results": collapse},
                      "global": {"existing_items": existing, "surviving_candidates": surviving, "results": verdicts}}
    write_json(vault / "System" / "Router" / "Reconciliation" / f"{shash}.json", reconciliation)
    # The ledger is written last: a crash before it replays the source, and markers dedupe items.
    write_json(ledger_path(vault, shash), dict(
        source_hash=shash, source_id=source_id, processed_at=datetime.now().astimezone().isoformat(),
        counts=counts, extraction=extraction, reconciliation=reconciliation))
    return counts


# --- Durable queue ----------------------------------------------------------------------------

class RouterQueue:
    """Published windows waiting for routing; independent of audio jobs so Hermes outages
    never block transcription. Failures back off and retry; text stays in the private data dir."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS routes (
                    id TEXT PRIMARY KEY, job TEXT NOT NULL, text TEXT NOT NULL, observed_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, error TEXT, result TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, job: str, windows: list[dict], observed_at: str) -> int:
        now = time.time()
        with self.connect() as db:
            return sum(db.execute(
                "INSERT OR IGNORE INTO routes(id,job,text,observed_at,created,updated) VALUES(?,?,?,?,?,?)",
                (f"{job}-{w['id']}", job, w["final_transcript"], observed_at, now, now)).rowcount for w in windows)

    def claim(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM routes WHERE state='pending' AND next_attempt<=? ORDER BY created LIMIT 1",
                             (time.time(),)).fetchone()
            return dict(row) if row else None

    def done(self, key: str, result: dict):
        with self.connect() as db:
            db.execute("UPDATE routes SET state='done',error=NULL,result=?,updated=? WHERE id=?",
                       (json.dumps(result), time.time(), key))

    def fail(self, route_row: dict, error: str, max_attempts: int = 6, base_seconds: float = 60):
        attempts = route_row["attempts"] + 1
        state = "failed" if attempts >= max_attempts else "pending"
        delay = min(base_seconds * 4 ** (attempts - 1), 6 * 3600)
        with self.connect() as db:
            db.execute("UPDATE routes SET state=?,attempts=?,next_attempt=?,error=?,updated=? WHERE id=?",
                       (state, attempts, time.time() + delay, error, time.time(), route_row["id"]))

    def retry_failed(self) -> int:
        with self.connect() as db:
            return db.execute("UPDATE routes SET state='pending',attempts=0,next_attempt=0,error=NULL "
                              "WHERE state='failed'").rowcount

    def snapshot(self) -> dict:
        with self.connect() as db:
            counts = {r["state"]: r["n"] for r in db.execute("SELECT state,count(*) n FROM routes GROUP BY state")}
            last = db.execute("SELECT error FROM routes WHERE error IS NOT NULL ORDER BY updated DESC LIMIT 1").fetchone()
        return {"pending": counts.get("pending", 0), "done": counts.get("done", 0),
                "failed": counts.get("failed", 0), "last_error": last["error"] if last else None}
