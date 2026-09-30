from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import wave
from second_brain.io import write_json

PROGRESS_FILE = None
COMMAND_TIMEOUT = 14400


def progress(stage):
    if PROGRESS_FILE:
        write_json(PROGRESS_FILE, {"stage": stage, "updated": time.time()})


from collections import Counter
from pathlib import Path

STOPWORDS = {
    "this",
    "that",
    "with",
    "from",
    "have",
    "what",
    "when",
    "where",
    "which",
    "would",
    "could",
    "should",
    "there",
    "their",
    "about",
    "just",
    "like",
    "then",
    "than",
    "your",
    "youre",
    "theyre",
    "were",
    "been",
    "being",
    "into",
    "some",
    "more",
    "very",
    "really",
    "because",
    "while",
    "will",
    "shall",
    "them",
    "they",
    "ours",
    "ourselves",
    "him",
    "her",
    "hers",
    "his",
    "and",
    "the",
    "for",
    "are",
    "was",
    "but",
    "not",
    "you",
    "our",
    "out",
    "all",
    "can",
    "did",
    "does",
    "had",
    "has",
    "its",
    "too",
    "also",
    "one",
    "two",
    "three",
    "yeah",
    "okay",
    "ok",
    "um",
    "uh",
}

IMPORTANCE_PATTERNS = {
    "decision": [
        r"\bdecid(?:e|ed|ing)\b",
        r"\blet'?s\b",
        r"\bwe(?:'re| are) going to\b",
        r"\bfinal(?:ly)? decided\b",
        r"\bthe plan is\b",
    ],
    "task": [
        r"\bneed to\b",
        r"\bhave to\b",
        r"\bremember to\b",
        r"\bdon'?t forget\b",
        r"\bschedule\b",
        r"\bemail\b",
        r"\bcall\b",
        r"\bsend\b",
        r"\bbook\b",
        r"\border\b",
        r"\bfix\b",
        r"\bfollow up\b",
    ],
    "commitment": [
        r"\bi(?:'ll| will)\b",
        r"\bwe(?:'ll| will)\b",
        r"\bpromise\b",
        r"\bby (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    ],
    "idea": [r"\bidea\b", r"\bwhat if\b", r"\bmaybe we should\b", r"\bi wonder\b", r"\bcould we\b", r"\bwe could\b"],
    "project": [
        r"\bproject\b",
        r"\blaunch\b",
        r"\bpricing\b",
        r"\brelease\b",
        r"\bbug\b",
        r"\bfeature\b",
        r"\bdeadline\b",
        r"\bclient\b",
    ],
}

DURABLE_PERSONAL_PATTERNS = [
    r"\b(?:likes?|loves?|dislikes?|hates?|prefers?)\b",
    r"\b(?:started|stopped|joined|quit|switched|changed)\b",
    r"\b(?:is|was|will be)\s+(?:allergic|available|unavailable|interested|attending|taking)\b",
]

PLANNING_PATTERNS = [
    r"\bappointment\b",
    r"\bmeeting\b",
    r"\baudition\b",
    r"\blesson\b",
    r"\bpractice\b",
    r"\bflight\b",
    r"\breservation\b",
    r"\bdeadline\b",
    r"\btryout\b",
    r"\binterview\b",
    r"\bregistration\b",
    r"\bapplication\b",
]

EPHEMERAL_FAMILY_PATTERNS = [
    r"\bwhat are you doing\b",
    r"\bwhat now\b",
    r"\bcome here\b",
    r"\bput (?:your|the) .{0,20}\bon\b",
    r"\btake (?:your|the) .{0,20}\boff\b",
    r"\bare you hungry\b",
    r"\bwant (?:some|a|the)?\s*(?:food|snack|breakfast|lunch|dinner|pancakes?)\b",
    r"\bgo to bed\b",
    r"\bbrush your teeth\b",
    r"\bclean (?:up|your room)\b",
    r"\bwhere are my\b",
    r"\bcan you get\b",
    r"\bpass me\b",
]

SMALL_TALK_PATTERNS = [
    r"\bhow are you\b",
    r"\bhow was your day\b",
    r"\bwhat'?s up\b",
    r"\bwhat are you up to\b",
    r"\bnice weather\b",
    r"\bthat'?s funny\b",
    r"\bno way\b",
    r"\bseriously\b",
]

ENTERTAINMENT_PATTERNS = [
    r"\bvideo game\b",
    r"\bplayed?\b.{0,25}\b(?:game|helldivers|starcraft)\b",
    r"\bwatch(?:ed|ing)?\b.{0,25}\b(?:movie|show|youtube|tv)\b",
    r"\bepisode\b",
    r"\bstreaming\b",
    r"\bgame\b",
]


def run(cmd, *, capture=True, check=True, env=None):
    print("[run] " + subprocess.list2cmdline([str(x) for x in cmd]), flush=True)
    proc = subprocess.run(
        [str(x) for x in cmd],
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
        timeout=COMMAND_TIMEOUT,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {subprocess.list2cmdline([str(x) for x in cmd])}\n"
            f"{proc.stderr[-4000:] if proc.stderr else ''}"
        )
    return proc


def ffprobe_duration(audio):
    proc = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            audio,
        ]
    )
    return float(proc.stdout.strip())


def detect_long_silences(audio, noise_db, duration):
    proc = run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            audio,
            "-af",
            f"silencedetect=noise={noise_db}dB:d={duration}",
            "-f",
            "null",
            "-",
        ],
        check=True,
    )
    text = (proc.stderr or "") + "\n" + (proc.stdout or "")
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", text)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", text)]
    silences = []
    end_i = 0
    for st in starts:
        while end_i < len(ends) and ends[end_i] < st:
            end_i += 1
        en = ends[end_i] if end_i < len(ends) else None
        if en is not None:
            silences.append((st, en))
            end_i += 1
    return silences


def complement_silences(total, silences, pad=1.5):
    spans = []
    cursor = 0.0
    for st, en in silences:
        if st > cursor:
            spans.append((max(0.0, cursor - pad), min(total, st + pad)))
        cursor = max(cursor, en)
    if cursor < total:
        spans.append((max(0.0, cursor - pad), total))
    # Drop tiny accidental regions and merge overlaps.
    spans = [x for x in spans if x[1] - x[0] >= 2.0]
    merged = []
    for st, en in spans:
        if merged and st <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], en))
        else:
            merged.append((st, en))
    return merged or [(0.0, total)]


def split_spans(spans, max_seconds):
    out = []
    for st, en in spans:
        cur = st
        while cur < en:
            nxt = min(en, cur + max_seconds)
            out.append((cur, nxt))
            cur = nxt
    return out


def extract_wav(audio, start, end, sample_rate, out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            audio,
            "-t",
            f"{max(0.01, end-start):.3f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(out),
        ]
    )
    return out


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def tokenize(text):
    return [
        x for x in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]{2,}", text.lower()) if x not in STOPWORDS and not x.isdigit()
    ]


def segments_text(segments):
    return " ".join((s.get("text") or "").strip() for s in segments if s.get("text"))


def format_segments(segments):
    rows = []
    for s in segments:
        rows.append(
            f"[{float(s['start']):.2f}-{float(s['end']):.2f}] " f"{s.get('speaker','S?')}: {s.get('text','').strip()}"
        )
    return "\n".join(rows)


def build_conversation_windows(segments, gap_seconds, max_seconds):
    if not segments:
        return []
    segments = sorted(segments, key=lambda x: (x["start"], x["end"]))
    windows = []
    current = [segments[0]]
    start = segments[0]["start"]

    for seg in segments[1:]:
        gap = max(0.0, seg["start"] - current[-1]["end"])
        proposed_duration = seg["end"] - start
        if gap > gap_seconds or proposed_duration > max_seconds:
            windows.append(current)
            current = [seg]
            start = seg["start"]
        else:
            current.append(seg)
    if current:
        windows.append(current)

    return [
        {
            "start": w[0]["start"],
            "end": w[-1]["end"],
            "segments": w,
        }
        for w in windows
    ]


def importance_heuristic(text, segments):
    lower = text.lower()
    score = 10
    hits = []
    category_hits = {}
    for category, patterns in IMPORTANCE_PATTERNS.items():
        n = sum(1 for p in patterns if re.search(p, lower))
        if n:
            category_hits[category] = n
            hits.append(category)
            score += min(22, 10 + (n - 1) * 5)

    if re.search(
        r"[$€£]\s?\d|\b\d{1,4}(?:\.\d+)?\s?(?:dollars?|percent|%|am|pm|minutes?|hours?|days?|weeks?)\b", lower
    ):
        score += 10
        hits.append("numbers/dates")

    words = text.split()
    if len(words) >= 80:
        score += 8
    elif len(words) >= 35:
        score += 4

    if len({s.get("speaker") for s in segments}) >= 2 and len(words) >= 30:
        score += 4

    return min(100, score), sorted(set(hits))


def uncertainty_heuristic(text, segments):
    if not text.strip():
        return 100, ["empty transcript"]

    score = 5
    reasons = []
    if re.search(r"(.)\1{12,}", text):
        return 100, ["repeated-character collapse"]

    alpha = sum(ch.isalpha() for ch in text)
    printable = sum(not ch.isspace() for ch in text)
    ratio = alpha / max(1, printable)
    if ratio < 0.6:
        score += 45
        reasons.append("low alphabetic-content ratio")

    repeated_words = len(re.findall(r"\b(\w+)\s+\1\b", text, flags=re.I))
    if repeated_words:
        score += min(20, repeated_words * 5)
        reasons.append("repeated words")

    texts = [(s.get("text") or "").strip().lower() for s in segments]
    duplicates = 0
    for i in range(1, len(texts)):
        if texts[i] and texts[i - 1] and texts[i] == texts[i - 1]:
            duplicates += 1
    if duplicates:
        score += min(25, duplicates * 10)
        reasons.append("duplicate adjacent turns")

    if len(segments) >= 4:
        short = sum(1 for s in segments if len((s.get("text") or "").split()) <= 3)
        short_ratio = short / len(segments)
        switches = sum(
            1 for i in range(1, len(segments)) if segments[i].get("speaker") != segments[i - 1].get("speaker")
        )
        switch_ratio = switches / max(1, len(segments) - 1)
        if short_ratio > 0.45 and switch_ratio > 0.55:
            score += 18
            reasons.append("rapid short speaker turns")

    if re.search(r"\b(?:inaudible|unintelligible|unknown speaker|speaker \?)\b", text, re.I):
        score += 25
        reasons.append("explicit uncertainty marker")

    return min(100, score), reasons


def _matches_any(patterns, lower):
    return any(re.search(p, lower, re.I) for p in patterns)


def memory_gate_heuristic(text, segments, importance_score, importance_signals):
    """
    Conservative durable-memory classifier.

    This intentionally treats novelty as irrelevant to the *keep/drop* decision.
    Novel household chatter can be novel without belonging in Second Brain.
    """
    lower = text.lower().strip()
    words = text.split()
    signals = set(importance_signals or [])

    contains = {
        "decision": "decision" in signals,
        "task": "task" in signals,
        "commitment": "commitment" in signals,
        "idea": "idea" in signals,
        "project_information": "project" in signals,
        "date_or_event": "numbers/dates" in signals or _matches_any(PLANNING_PATTERNS, lower),
        "personal_durable_fact": _matches_any(DURABLE_PERSONAL_PATTERNS, lower),
    }

    # Determine a primary conversation type.
    if contains["decision"]:
        ctype = "decision"
    elif contains["task"] or contains["commitment"]:
        ctype = "task_commitment"
    elif contains["project_information"]:
        ctype = "project_work"
    elif contains["idea"]:
        ctype = "idea"
    elif contains["date_or_event"]:
        ctype = "planning_logistics"
    elif contains["personal_durable_fact"]:
        ctype = "personal_durable_fact"
    elif _matches_any(ENTERTAINMENT_PATTERNS, lower):
        ctype = "entertainment_media"
    elif _matches_any(EPHEMERAL_FAMILY_PATTERNS, lower):
        ctype = "family_chatter"
    elif _matches_any(SMALL_TALK_PATTERNS, lower):
        ctype = "small_talk"
    elif len(words) < 8:
        ctype = "other_ephemeral"
    else:
        ctype = "other"

    durability = 8
    actionability = 5
    retrieval = max(5, int(importance_score * 0.35))
    reasons = []

    # Strong durable signals.
    if contains["decision"]:
        durability = max(durability, 92)
        retrieval = max(retrieval, 88)
        reasons.append("decision")
    if contains["task"]:
        durability = max(durability, 86)
        actionability = max(actionability, 92)
        retrieval = max(retrieval, 82)
        reasons.append("task")
    if contains["commitment"]:
        durability = max(durability, 88)
        actionability = max(actionability, 90)
        retrieval = max(retrieval, 84)
        reasons.append("commitment")
    if contains["project_information"]:
        durability = max(durability, 78)
        retrieval = max(retrieval, 78)
        reasons.append("project information")
    if contains["idea"]:
        durability = max(durability, 70)
        retrieval = max(retrieval, 70)
        reasons.append("idea")
    if contains["date_or_event"]:
        durability = max(durability, 76)
        actionability = max(actionability, 70)
        retrieval = max(retrieval, 80)
        reasons.append("date/event/logistics")
    if contains["personal_durable_fact"]:
        durability = max(durability, 72)
        retrieval = max(retrieval, 76)
        reasons.append("durable personal fact")

    # More substantial discussion modestly improves retrieval value.
    if len(words) >= 80:
        retrieval += 7
    elif len(words) >= 35:
        retrieval += 3

    # Ephemeral categories are explicitly penalized unless a durable signal exists.
    durable_signal = any(contains.values())
    if ctype in {"family_chatter", "small_talk", "entertainment_media", "other_ephemeral"} and not durable_signal:
        durability = min(durability, 18)
        retrieval = min(retrieval, 22)
        actionability = min(actionability, 12)
        reasons.append(f"ephemeral type: {ctype}")

    return {
        "conversation_type": ctype,
        "durability": max(0, min(100, int(durability))),
        "actionability": max(0, min(100, int(actionability))),
        "retrieval_value": max(0, min(100, int(retrieval))),
        "contains": contains,
        "durable_signal": durable_signal,
        "reasons": reasons,
    }


def merge_memory_gate(heuristic_gate, llm_gate=None):
    """
    Combine deterministic gate with Hermes when available.
    A positive durable signal from either source is preserved.
    """
    if not llm_gate:
        return dict(heuristic_gate)

    h_contains = heuristic_gate.get("contains", {})
    l_contains = llm_gate.get("contains", {}) or {}
    contains = {key: bool(h_contains.get(key) or l_contains.get(key)) for key in set(h_contains) | set(l_contains)}

    ctype = llm_gate.get("conversation_type") or heuristic_gate.get("conversation_type", "other")
    return {
        "conversation_type": ctype,
        "durability": round(
            0.4 * heuristic_gate.get("durability", 0)
            + 0.6 * llm_gate.get("durability", heuristic_gate.get("durability", 0))
        ),
        "actionability": round(
            0.4 * heuristic_gate.get("actionability", 0)
            + 0.6 * llm_gate.get("actionability", heuristic_gate.get("actionability", 0))
        ),
        "retrieval_value": round(
            0.4 * heuristic_gate.get("retrieval_value", 0)
            + 0.6 * llm_gate.get("retrieval_value", heuristic_gate.get("retrieval_value", 0))
        ),
        "contains": contains,
        "durable_signal": any(contains.values()),
        "reasons": list(heuristic_gate.get("reasons", []))
        + ([llm_gate.get("memory_reason")] if llm_gate.get("memory_reason") else []),
    }


def evaluate_memory_gate(cfg, gate, importance, word_count):
    if not cfg.get("memory_gate_enabled", True):
        return True, "memory gate disabled"

    if word_count < cfg.get("memory_gate_min_words", 10):
        return False, "too little content"

    ctype = gate.get("conversation_type", "other")
    durability = int(gate.get("durability", 0))
    retrieval = int(gate.get("retrieval_value", 0))
    durable_signal = bool(gate.get("durable_signal"))

    ephemeral_types = {"family_chatter", "small_talk", "entertainment_media", "background_audio", "other_ephemeral"}

    # A real durable signal wins even inside family conversation.
    if durable_signal:
        return True, "contains durable information"

    # Ephemeral categories need an unusually strong override.
    if ctype in ephemeral_types:
        if importance >= cfg.get("memory_gate_importance_override", 90) and retrieval >= cfg.get(
            "memory_gate_importance_retrieval_floor", 48
        ):
            return True, "ephemeral category overridden by unusually high retrieval value"
        return False, f"ephemeral conversation type: {ctype}"

    if durability >= cfg.get("memory_gate_durability_threshold", 58) and retrieval >= cfg.get(
        "memory_gate_retrieval_threshold", 55
    ):
        return True, "durability and retrieval thresholds met"

    if importance >= cfg.get("memory_gate_importance_override", 90) and retrieval >= cfg.get(
        "memory_gate_importance_retrieval_floor", 48
    ):
        return True, "high-importance override"

    return False, "no durable signal and memory thresholds not met"


def should_escalate_to_vibe7(cfg, importance, uncertainty, heuristic_scores, memory_gate):
    """
    Avoid spending 7B compute on content already classified as ephemeral.
    Catastrophically uncertain ASR still gets a rescue pass in case MOSS hid
    durable content.
    """
    if uncertainty >= cfg.get("vibe7_rescue_uncertainty_threshold", 92):
        return True

    pre_keep, _ = evaluate_memory_gate(
        cfg,
        memory_gate,
        importance,
        word_count=max(cfg.get("memory_gate_min_words", 10), 20),
    )
    if not pre_keep and importance < 70:
        return False

    return choose_tier(cfg, importance, uncertainty, heuristic_scores) == "vibe7"


def load_vault_index(vault_path, max_notes):
    path = Path(vault_path)
    if not path.exists():
        return []
    notes = []
    for p in path.rglob("*.md"):
        if len(notes) >= max_notes:
            break
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")[:5000]
        except Exception:
            continue
        terms = set(tokenize(p.stem + " " + txt))
        if terms:
            notes.append(
                {
                    "path": str(p),
                    "terms": terms,
                    "snippet": txt[:1200],
                }
            )
    return notes


def vault_novelty(text, vault_index):
    query = set(tokenize(text))
    if not query or not vault_index:
        return 50, []
    scored = []
    for note in vault_index:
        inter = len(query & note["terms"])
        if not inter:
            continue
        coverage = inter / max(1, len(query))
        scored.append((coverage, note))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:3]
    max_coverage = top[0][0] if top else 0.0
    novelty = int(round(100 * (1.0 - min(1.0, max_coverage))))
    context = [
        {
            "path": x[1]["path"],
            "coverage": round(x[0], 3),
            "snippet": x[1]["snippet"],
        }
        for x in top
    ]
    return novelty, context


def hermes_score(cfg, transcript, heuristic, vault_context):
    if not cfg.get("hermes_scoring_enabled"):
        return None
    url = cfg.get("hermes_url")
    if not url or "YOUR-PI-IP" in url:
        return None
    key = os.environ.get(cfg.get("hermes_api_key_env", "HERMES_API_KEY"), "")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    nearby = (
        "\n\n".join(f"NOTE: {x['path']}\n{x['snippet'][:800]}" for x in vault_context[:3]) or "(no nearby vault notes)"
    )

    prompt = f"""Score this conversation window for a personal second-brain ingestion pipeline.

Return ONLY valid JSON:
{{
  "importance": integer 0-100,
  "novelty": integer 0-100,
  "asr_uncertainty": integer 0-100,
  "categories": ["decision","task","idea","personal_fact","project","planning","other"],
  "reason": "brief explanation",
  "conversation_type": "project_work|decision|task_commitment|planning_logistics|idea|personal_durable_fact|family_chatter|small_talk|entertainment_media|background_audio|other_ephemeral|other",
  "durability": integer 0-100,
  "actionability": integer 0-100,
  "retrieval_value": integer 0-100,
  "contains": {
    "decision": boolean,
    "task": boolean,
    "commitment": boolean,
    "idea": boolean,
    "project_information": boolean,
    "date_or_event": boolean,
    "personal_durable_fact": boolean
  },
  "memory_keep": boolean,
  "memory_reason": "brief explanation of why this does or does not belong in durable memory"
}}

Definitions:
- importance: likelihood that the user will benefit from remembering/retrieving this later.
- novelty: how much durable information appears new relative to nearby vault notes.
- asr_uncertainty: likelihood the transcript has consequential transcription or speaker-boundary errors. Do not penalize casual grammar by itself.
- durability: whether the information will still matter later, not whether the conversation is interesting now.
- retrieval_value: likelihood the user will intentionally want to retrieve this later.
- family chatter / small talk / entertainment should normally NOT enter durable memory unless they contain a decision, task, commitment, future event, durable personal fact, project information, or a genuinely reusable idea.
- novelty alone is NOT a reason to keep something. Novel ephemeral chatter should still be dropped.

Heuristic pre-scores:
{json.dumps(heuristic, indent=2)}

Nearby existing notes:
{nearby}

Transcript:
{transcript}
"""
    body = {
        "model": cfg.get("hermes_model", "default"),
        "messages": [
            {"role": "system", "content": "You are a conservative routing scorer. Output JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=cfg.get("hermes_timeout_seconds", 90)) as resp:
        obj = json.loads(resp.read().decode("utf-8"))
    content = obj["choices"][0]["message"]["content"].strip()
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError("Hermes scorer returned no JSON object")
    result = json.loads(m.group(0))
    return {
        "importance": max(0, min(100, int(result["importance"]))),
        "novelty": max(0, min(100, int(result["novelty"]))),
        "asr_uncertainty": max(0, min(100, int(result["asr_uncertainty"]))),
        "categories": result.get("categories", []),
        "reason": str(result.get("reason", "")),
        "conversation_type": str(result.get("conversation_type", "other")),
        "durability": max(0, min(100, int(result.get("durability", 0)))),
        "actionability": max(0, min(100, int(result.get("actionability", 0)))),
        "retrieval_value": max(0, min(100, int(result.get("retrieval_value", 0)))),
        "contains": result.get("contains", {}) if isinstance(result.get("contains", {}), dict) else {},
        "memory_keep": bool(result.get("memory_keep", False)),
        "memory_reason": str(result.get("memory_reason", "")),
    }


def normalize_hotword_key(value):
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def build_hotword_catalog(cfg):
    """
    Build a small catalog from stable vault entity-note names.  We prefer note
    titles because they contain canonical spellings, unlike first-pass ASR.
    """
    vault = Path(cfg.get("vault_path", ""))
    if not vault.exists():
        return []

    allowed = set(cfg.get("hotword_vault_folders") or [])
    max_len = int(cfg.get("hotword_max_note_title_length", 80))
    out = []
    seen = set()

    for folder in allowed:
        base = vault / folder
        if not base.exists():
            continue
        for p in base.rglob("*.md"):
            term = p.stem.strip()
            if not term or len(term) > max_len:
                continue
            key = normalize_hotword_key(term)
            if len(key) < 2 or key in seen:
                continue
            seen.add(key)
            out.append(term)

    return out


def derive_hotwords(cfg, transcript, vault_context, hotword_catalog):
    """
    Per-window hotwords:
      1. configured always-use domain terms
      2. titles of the nearest vault notes
      3. entity-note titles whose tokens overlap the first-pass transcript

    The list is intentionally bounded; flooding context_info with the entire
    vault would make the bias signal noisy.
    """
    candidates = []
    seen = set()

    def add(term, priority):
        term = str(term).strip()
        key = normalize_hotword_key(term)
        if not key or key in seen:
            return
        seen.add(key)
        candidates.append((priority, term))

    for term in cfg.get("static_hotwords") or []:
        add(term, 100)

    for i, item in enumerate(vault_context[:5]):
        path = item.get("path")
        if path:
            add(Path(path).stem, 90 - i)

    transcript_tokens = set(tokenize(transcript))
    for term in hotword_catalog:
        key_tokens = set(tokenize(term))
        if key_tokens and transcript_tokens & key_tokens:
            # Stronger overlap first.  Exact-ish multi-token entities get a bump.
            overlap = len(transcript_tokens & key_tokens)
            coverage = overlap / max(1, len(key_tokens))
            add(term, 60 + int(20 * coverage) + min(10, overlap * 3))

    candidates.sort(key=lambda x: (-x[0], x[1].lower()))

    max_terms = int(cfg.get("hotword_max_terms", 32))
    max_chars = int(cfg.get("hotword_max_chars", 700))
    selected = []
    chars = 0

    for _, term in candidates:
        extra = len(term) + (1 if selected else 0)
        if len(selected) >= max_terms or chars + extra > max_chars:
            break
        selected.append(term)
        chars += extra

    return selected


def has_critical_signal(heuristic_scores):
    signals = set(heuristic_scores.get("importance_signals") or [])
    return bool(signals & {"decision", "task", "commitment", "numbers/dates"})


def choose_tier(cfg, importance, uncertainty, heuristic_scores=None):
    """
    Only two ASR tiers:
      - MOSS by default
      - VibeVoice Streaming 7B when the expected value of a second pass is high
    """
    critical = has_critical_signal(heuristic_scores or {})

    if uncertainty >= cfg["vibe7_uncertainty_threshold"]:
        return "vibe7"

    if importance >= cfg["vibe7_importance_threshold"] and uncertainty >= cfg["vibe7_important_uncertainty_threshold"]:
        return "vibe7"

    if (
        critical
        and importance >= cfg["vibe7_critical_signal_importance_threshold"]
        and uncertainty >= cfg["vibe7_critical_signal_uncertainty_threshold"]
    ):
        return "vibe7"

    return "moss"


def should_route(cfg, importance, novelty, words):
    if words < cfg["router_min_words"]:
        return False
    return importance >= cfg["router_importance_threshold"] or novelty >= cfg["router_novelty_threshold"]


def parse_vibe_segments(result, start, end, window_id):
    segs = result.get("segments") or []
    rows = []
    for s in segs:
        offset = float(result.get("context_start") or 0)
        st = offset + float(s.get("approx_start", 0))
        en = offset + float(s.get("approx_end", st))
        if (st + en) / 2 < start or (st + en) / 2 >= end:
            continue
        speaker = window_id + ":" + str(s["speaker"] if s.get("speaker") is not None else "S?")
        text = (s.get("text") or "").strip()
        if text:
            rows.append(dict(start=max(start, st), end=min(end, en), speaker=speaker, text=text, approximate=True))
    return rows


def parse_vibe_text(result, start, end, window_id):
    return "\n".join(f"{s['speaker']}: {s['text']}" for s in parse_vibe_segments(result, start, end, window_id))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--skip-vibe7", action="store_true")
    ap.add_argument("--no-hermes", action="store_true")
    ap.add_argument("--output-dir")
    ap.add_argument("--progress-file")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    global PROGRESS_FILE, COMMAND_TIMEOUT
    PROGRESS_FILE = Path(args.progress_file) if args.progress_file else None
    COMMAND_TIMEOUT = float(cfg.get("command_timeout_seconds", 14400))
    if args.no_hermes:
        cfg["hermes_scoring_enabled"] = False

    audio = str(Path(args.audio).resolve())
    total_duration = ffprobe_duration(audio)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.output_dir) if args.output_dir else Path(cfg["work_root"]) / (stamp + "-" + str(time.time_ns()))
    coarse_dir = run_dir / "coarse"
    windows_dir = run_dir / "windows"
    refined_dir = run_dir / "refined"
    router_dir = run_dir / "router_queue"
    filtered_dir = run_dir / "filtered_out"
    for d in (coarse_dir, windows_dir, refined_dir, router_dir, filtered_dir):
        d.mkdir(parents=True, exist_ok=True)

    manifest = {
        "version": 3,
        "audio": audio,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "fallbacks": [],
        "total_duration_seconds": total_duration,
        "config": cfg,
        "coarse_chunks": [],
        "windows": [],
    }

    # Stage 0: only use LONG silence to skip dead air. This is intentionally
    # conservative, not aggressive VAD.
    progress("segmentation")
    print("=== Stage 0: long-silence segmentation ===", flush=True)
    silences = detect_long_silences(audio, cfg["silence_noise_db"], cfg["long_silence_seconds"])
    active_spans = complement_silences(total_duration, silences)
    coarse_spans = split_spans(active_spans, cfg["coarse_max_seconds"])
    manifest["active_duration_seconds"] = sum(en - st for st, en in active_spans)
    manifest["long_silences"] = silences
    print(
        f"Audio {total_duration/3600:.2f}h -> {manifest['active_duration_seconds']/3600:.2f}h "
        f"outside long silences -> {len(coarse_spans)} MOSS chunks",
        flush=True,
    )

    # Stage 1: MOSS everything meaningful.
    progress("transcribing")
    print("=== Stage 1: MOSS first pass ===", flush=True)
    moss_exe = str(Path(cfg["moss_cpp_engine_dir"]) / ("moss-transcribe.exe" if os.name == "nt" else "moss-transcribe"))
    moss_runner = str(Path(__file__).parent / "runners" / "moss_cpp_runner.py")
    all_segments = []

    for idx, (st, en) in enumerate(coarse_spans):
        cid = f"c{idx:04d}"
        wav = coarse_dir / f"{cid}.wav"
        jout = coarse_dir / f"{cid}.json"
        extract_wav(audio, st, en, 16000, wav)
        run(
            [
                sys.executable,
                moss_runner,
                "--command-json",
                json.dumps(cfg.get("moss_command", [moss_exe])),
                "--model",
                cfg["moss_model"],
                "--audio",
                str(wav),
                "--output",
                str(jout),
                "--threads",
                str(cfg["moss_threads"]),
                "--device",
                cfg.get("moss_device", "cpu"),
                "--max-new",
                str(cfg["moss_max_new"]),
            ],
            capture=False,
        )

        obj = load_json(jout)
        segs = []
        for seg in obj.get("segments", []):
            abs_seg = dict(seg)
            abs_seg["start"] = st + float(seg["start"])
            abs_seg["end"] = st + float(seg["end"])
            abs_seg["coarse_chunk"] = cid
            abs_seg["speaker"] = cid + ":" + str(seg.get("speaker", "S?"))
            segs.append(abs_seg)
            all_segments.append(abs_seg)

        manifest["coarse_chunks"].append(
            {
                "id": cid,
                "start": st,
                "end": en,
                "audio": str(wav),
                "moss_json": str(jout),
                "segment_count": len(segs),
            }
        )

    all_segments.sort(key=lambda s: (s["start"], s["end"]))
    (run_dir / "moss_all_segments.json").write_text(
        json.dumps(all_segments, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "moss_transcript.txt").write_text(format_segments(all_segments), encoding="utf-8")

    # Stage 2: natural conversation windows.
    progress("scoring")
    print("=== Stage 2: conversation windows + scoring ===", flush=True)
    window_groups = build_conversation_windows(
        all_segments,
        cfg["conversation_gap_seconds"],
        cfg["conversation_max_seconds"],
    )

    vault_index = []
    hotword_catalog = build_hotword_catalog(cfg)
    print(f"[hotwords] indexed {len(hotword_catalog)} canonical vault entity names", flush=True)

    if cfg.get("scan_vault_for_novelty") and cfg.get("vault_path"):
        print("[vault] indexing notes for novelty context...", flush=True)
        vault_index = load_vault_index(cfg["vault_path"], cfg["max_vault_notes"])
        print(f"[vault] indexed {len(vault_index)} notes", flush=True)

    for i, w in enumerate(window_groups):
        wid = f"w{i:04d}"
        text = segments_text(w["segments"])
        imp_h, imp_hits = importance_heuristic(text, w["segments"])
        unc_h, unc_reasons = uncertainty_heuristic(text, w["segments"])
        nov_h, vault_ctx = vault_novelty(text, vault_index)
        heuristic = {
            "importance": imp_h,
            "novelty": nov_h,
            "asr_uncertainty": unc_h,
            "importance_signals": imp_hits,
            "uncertainty_signals": unc_reasons,
        }
        memory_h = memory_gate_heuristic(text, w["segments"], imp_h, imp_hits)

        llm = None
        if cfg.get("hermes_scoring_enabled"):
            try:
                llm = hermes_score(cfg, format_segments(w["segments"]), heuristic, vault_ctx)
            except Exception as exc:
                manifest["fallbacks"].append(
                    {"window": wid, "from": "hermes", "to": "heuristics", "reason": type(exc).__name__}
                )
                print(f"[Hermes scorer] {wid} failed: {type(exc).__name__}; using heuristics", flush=True)

        if llm:
            importance = round(0.35 * imp_h + 0.65 * llm["importance"])
            novelty = round(0.25 * nov_h + 0.75 * llm["novelty"])
            uncertainty = round(0.35 * unc_h + 0.65 * llm["asr_uncertainty"])
        else:
            importance, novelty, uncertainty = imp_h, nov_h, unc_h

        llm_memory = None
        if llm:
            llm_memory = {
                "conversation_type": llm.get("conversation_type"),
                "durability": llm.get("durability", 0),
                "actionability": llm.get("actionability", 0),
                "retrieval_value": llm.get("retrieval_value", 0),
                "contains": llm.get("contains", {}),
                "memory_keep": llm.get("memory_keep", False),
                "memory_reason": llm.get("memory_reason", ""),
            }

        preliminary_memory_gate = merge_memory_gate(memory_h, llm_memory)
        words = len(text.split())
        pre_keep, pre_keep_reason = evaluate_memory_gate(cfg, preliminary_memory_gate, importance, words)

        tier = (
            "vibe7"
            if should_escalate_to_vibe7(cfg, importance, uncertainty, heuristic, preliminary_memory_gate)
            else "moss"
        )
        hotwords = derive_hotwords(cfg, text, vault_ctx, hotword_catalog)

        item = {
            "id": wid,
            "start": w["start"],
            "end": w["end"],
            "duration_seconds": w["end"] - w["start"],
            "word_count": words,
            "moss_segments": w["segments"],
            "moss_transcript": format_segments(w["segments"]),
            "heuristic_scores": heuristic,
            "hermes_scores": llm,
            "scores": {
                "importance": importance,
                "novelty": novelty,
                "asr_uncertainty": uncertainty,
            },
            "asr_tier": tier,
            "hotwords": hotwords,
            "preliminary_memory_gate": preliminary_memory_gate,
            "preliminary_memory_keep": pre_keep,
            "preliminary_memory_reason": pre_keep_reason,
            "vault_context": [{"path": x["path"], "coverage": x["coverage"]} for x in vault_ctx],
        }
        manifest["windows"].append(item)
        (windows_dir / f"{wid}.json").write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")

        print(
            f"{wid} {w['start']/60:.1f}-{w['end']/60:.1f}m "
            f"I={importance} N={novelty} U={uncertainty} "
            f"M={preliminary_memory_gate['conversation_type']}/"
            f"{preliminary_memory_gate['durability']}/"
            f"{preliminary_memory_gate['retrieval_value']} "
            f"keep={'yes' if pre_keep else 'no'} -> {tier}",
            flush=True,
        )

    # Stage 3: extract only selected 7B windows.  All windows are batched so
    # the expensive 7B checkpoint loads once for the entire run.
    progress("refining")
    print("=== Stage 3: selective Streaming-7B escalation ===", flush=True)
    jobs7 = []

    for item in manifest["windows"]:
        if item["asr_tier"] != "vibe7" or args.skip_vibe7:
            continue

        pad = cfg["escalation_padding_seconds"]
        st = max(0.0, item["start"] - pad)
        en = min(total_duration, item["end"] + pad)

        wav = refined_dir / f"{item['id']}-vibe7.wav"
        extract_wav(audio, st, en, 24000, wav)

        jout = refined_dir / f"{item['id']}-vibe7.json"
        job = {
            "window_id": item["id"],
            "audio": str(wav),
            "output": str(jout),
            "context_start": st,
            "context_end": en,
            "hotwords": item.get("hotwords") or [],
        }

        item["escalation_audio"] = str(wav)
        item["escalation_json"] = str(jout)
        jobs7.append(job)

    vibe_python = cfg.get("vibe_python") or str(Path(cfg["vibe_repo"]) / ".venv" / "Scripts" / "python.exe")
    vibe_runner = str(Path(__file__).parent / "runners" / "vibe_batch_runner.py")

    if jobs7 and not args.skip_vibe7:
        jobs_path = refined_dir / "vibe7-jobs.json"
        jobs_path.write_text(json.dumps(jobs7, indent=2), encoding="utf-8")
        run(
            [
                vibe_python,
                vibe_runner,
                "--jobs",
                str(jobs_path),
                "--model",
                cfg["vibe_7b_model"],
                "--device",
                cfg.get("vibe_7b_device", "cpu"),
                "--dtype",
                cfg.get("vibe_7b_dtype", "float32"),
                "--max-new-tokens",
                str(cfg["vibe_max_new_tokens"]),
            ],
            capture=False,
        )
    else:
        print("[Vibe 7B] no selected windows", flush=True)

    # Stage 4: select final transcript and create router queue.
    progress("routing")
    print("=== Stage 4: final transcript + router queue ===", flush=True)
    router_count = 0
    tier_counts = Counter()
    tier_seconds = Counter()

    for item in manifest["windows"]:
        tier = item["asr_tier"]
        tier_counts[tier] += 1
        tier_seconds[tier] += item["duration_seconds"]

        final_engine = "moss"
        final_text = item["moss_transcript"]
        final_segments = item["moss_segments"]

        ref_path = item.get("escalation_json")
        if ref_path and Path(ref_path).exists():
            ref = load_json(ref_path)
            item["refinement_status"] = ref.get("status")
            if ref.get("status") == "ok":
                candidate = parse_vibe_text(ref, item["start"], item["end"], item["id"])
                if candidate.strip():
                    final_text = candidate
                    final_engine = tier
                    final_segments = parse_vibe_segments(ref, item["start"], item["end"], item["id"])

        if ref_path and final_engine == "moss":
            manifest["fallbacks"].append(
                {"window": item["id"], "from": "vibe7", "to": "moss", "reason": "refinement unavailable or empty"}
            )
        item["final_engine"] = final_engine
        item["final_transcript"] = final_text
        item["final_segments"] = final_segments

        final_memory_h = memory_gate_heuristic(
            final_text,
            item.get("moss_segments", []),
            item["scores"]["importance"],
            item.get("heuristic_scores", {}).get("importance_signals", []),
        )
        llm_memory = None
        if item.get("hermes_scores"):
            hs = item["hermes_scores"]
            llm_memory = {
                "conversation_type": hs.get("conversation_type"),
                "durability": hs.get("durability", 0),
                "actionability": hs.get("actionability", 0),
                "retrieval_value": hs.get("retrieval_value", 0),
                "contains": hs.get("contains", {}),
                "memory_keep": hs.get("memory_keep", False),
                "memory_reason": hs.get("memory_reason", ""),
            }

        final_memory_gate = merge_memory_gate(final_memory_h, llm_memory)
        memory_keep, memory_reason = evaluate_memory_gate(
            cfg,
            final_memory_gate,
            item["scores"]["importance"],
            len(final_text.split()),
        )

        item["memory_gate"] = final_memory_gate
        item["memory_keep"] = memory_keep
        item["memory_reason"] = memory_reason
        item["route_to_knowledge_router"] = memory_keep

        final_path = windows_dir / f"{item['id']}.final.txt"
        final_path.write_text(final_text + "\n", encoding="utf-8")
        item["final_transcript_path"] = str(final_path)

        if item["route_to_knowledge_router"]:
            router_count += 1
            score = item["scores"]
            mg = item["memory_gate"]
            router_path = router_dir / f"{item['id']}.txt"
            router_path.write_text(
                "\n".join(
                    [
                        f"Source audio: {audio}",
                        f"Window: {item['start']:.2f}-{item['end']:.2f} seconds",
                        f"ASR engine: {final_engine}",
                        f"Importance: {score['importance']}",
                        f"Novelty: {score['novelty']}",
                        f"ASR uncertainty (first pass): {score['asr_uncertainty']}",
                        f"Conversation type: {mg['conversation_type']}",
                        f"Durability: {mg['durability']}",
                        f"Retrieval value: {mg['retrieval_value']}",
                        f"Actionability: {mg['actionability']}",
                        f"Memory gate: KEEP ({memory_reason})",
                        "",
                        "TRANSCRIPT",
                        final_text,
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            item["router_input"] = str(router_path)
        elif cfg.get("memory_gate_archive_filtered", True):
            filtered_path = filtered_dir / f"{item['id']}.json"
            filtered_path.write_text(
                json.dumps(
                    {
                        "window_id": item["id"],
                        "start": item["start"],
                        "end": item["end"],
                        "final_engine": final_engine,
                        "scores": item["scores"],
                        "memory_gate": item["memory_gate"],
                        "memory_reason": memory_reason,
                        "transcript_path": str(final_path),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            item["filtered_record"] = str(filtered_path)

    # Keep bounded PCM clips for human review. Voice profiles are owned by the
    # service; model extraction stays inside this supervised job subprocess.
    progress("speakers")
    from second_brain.speaker_audio import prepare

    prepare(manifest, audio, run_dir, cfg)

    # Persist final manifest.
    filtered_count = sum(1 for x in manifest["windows"] if not x.get("route_to_knowledge_router"))
    memory_type_counts = Counter(
        x.get("memory_gate", {}).get("conversation_type", "unknown") for x in manifest["windows"]
    )

    manifest["summary"] = {
        "window_count": len(manifest["windows"]),
        "router_window_count": router_count,
        "filtered_window_count": filtered_count,
        "memory_type_counts": dict(memory_type_counts),
        "asr_tier_counts": dict(tier_counts),
        "asr_tier_seconds": {k: round(v, 2) for k, v in tier_seconds.items()},
        "vibe7_selected_minutes": round(tier_seconds["vibe7"] / 60, 2),
        "active_minutes": round(manifest["active_duration_seconds"] / 60, 2),
    }
    write_json(run_dir / "manifest.json", manifest)

    # Human report.
    report = [
        "# Adaptive Audio Run",
        "",
        f"- Source: `{audio}`",
        f"- Total audio: {total_duration/60:.1f} min",
        f"- Audio outside long silences: {manifest['active_duration_seconds']/60:.1f} min",
        f"- Natural conversation windows: {len(manifest['windows'])}",
        f"- Durable-memory queue: {router_count} windows",
        f"- Filtered as ephemeral/non-durable: {filtered_count} windows",
        f"- MOSS only: {tier_counts['moss']} windows / {tier_seconds['moss']/60:.1f} min",
        f"- VibeVoice Streaming 7B selected: {tier_counts['vibe7']} windows / {tier_seconds['vibe7']/60:.1f} min",
        "",
        "## Selected windows",
        "",
        "| Window | Time | I | N | U | Type | Durability | Retrieval | ASR | Memory |",
        "|---|---:|---:|---:|---:|---|---:|---:|---|---|",
    ]
    for item in manifest["windows"]:
        sc = item["scores"]
        mg = item.get("memory_gate", {})
        report.append(
            f"| {item['id']} | {item['start']/60:.1f}-{item['end']/60:.1f}m | "
            f"{sc['importance']} | {sc['novelty']} | {sc['asr_uncertainty']} | "
            f"{mg.get('conversation_type','?')} | {mg.get('durability','?')} | "
            f"{mg.get('retrieval_value','?')} | {item['final_engine']} | "
            f"{'KEEP' if item['route_to_knowledge_router'] else 'drop'} |"
        )

    report += [
        "",
        "## Durable-memory policy",
        "",
        "- MOSS is the default transcript.",
        "- Family chatter, small talk, entertainment chatter, and other ephemeral windows are dropped unless they contain durable information.",
        "- Novelty alone never causes a window to enter Second Brain.",
        "- Decisions, tasks, commitments, future events, project information, durable personal facts, and reusable ideas can pass the memory gate.",
        "- VibeVoice Streaming 7B is used only when a potentially useful window needs ASR refinement, plus a catastrophic-ASR rescue path.",
        "- Selected 7B windows receive bounded vault-derived hotwords through context_info.",
        "- Filtered windows remain in the transcript archive and get an audit record under filtered_out; they do not enter router_queue.",
        "",
        f"Manifest: `{run_dir / 'manifest.json'}`",
        f"Router queue: `{router_dir}`",
    ]
    (run_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    print("", flush=True)
    print("=== COMPLETE ===", flush=True)
    print(f"Run directory: {run_dir}", flush=True)
    print(f"Report:        {run_dir / 'report.md'}", flush=True)
    print(f"Manifest:      {run_dir / 'manifest.json'}", flush=True)
    print(f"Router queue:  {router_dir}", flush=True)


if __name__ == "__main__":
    main()
