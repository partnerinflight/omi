"""The owner's status page: worker freshness, backlog counts and paired phones. Counts and
names only, never content. Served behind Traefik's Google forward auth (see app.status_page)."""
from datetime import datetime, timezone
from html import escape

STALE_SECONDS = 300  # the Windows bridge reports every 60 s by default

STYLE = """
:root { color-scheme: light dark; --bg: #fff; --fg: #1c1c1e; --muted: #6e6e73; --card: #f2f2f7;
        --ok: #248a3d; --bad: #d70015; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #000; --fg: #f2f2f7; --muted: #98989d; --card: #1c1c1e; --ok: #30d158; --bad: #ff453a; } }
body { background: var(--bg); color: var(--fg); font: 15px/1.4 -apple-system, system-ui, sans-serif;
       max-width: 720px; margin: 0 auto; padding: 16px; }
h1 { font-size: 22px; } h2 { font-size: 13px; text-transform: uppercase; color: var(--muted); margin-top: 24px; }
section { background: var(--card); border-radius: 12px; padding: 12px 16px; margin-bottom: 8px; }
.ok { color: var(--ok); } .bad { color: var(--bad); } .muted { color: var(--muted); }
"""


def ago(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "never"
    seconds = max(0, int((now - when).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return f"{seconds} s ago"


def render_status(summary: dict, phones: list[dict], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    workers = []
    for w in summary["workers"]:
        seen = datetime.fromisoformat(w["last_seen_at"]) if w["last_seen_at"] else None
        fresh = seen is not None and (now - seen).total_seconds() < STALE_SECONDS
        status = w["status"] or {}
        jobs = status.get("jobs") or {}
        detail = " · ".join(f"{escape(str(k))} {escape(str(v))}" for k, v in sorted(jobs.items()))
        extra = [f"{escape(str(status[k]))} {label}" for k, label in
                 (("routes_pending", "routes pending"), ("export_conflicts", "export conflicts")) if status.get(k)]
        workers.append(
            f'<section><b>{escape(w["name"])}</b> <span class="{"ok" if fresh else "bad"}">'
            f'{"reporting" if fresh else "not reporting"}</span> <span class="muted">· last seen {ago(seen, now)}</span>'
            f'<div class="muted">{" · ".join(filter(None, [detail, *extra])) or "no job counts reported"}</div></section>')
    counts = summary["counts"]
    records = " · ".join(f"{escape(str(n))} {escape(k)}" for k, n in sorted(counts.items())) or "none yet"
    oldest = summary["oldest_pending_command_at"]
    waiting = ago(datetime.fromisoformat(oldest), now) if oldest else None
    phone_rows = "".join(f'<section>{escape(p["name"])} <span class="muted">· paired {p["created_at"]:%Y-%m-%d}</span></section>'
                         for p in phones) or '<section class="muted">No phones paired</section>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30"><title>Second Brain Status</title><style>{STYLE}</style></head>
<body><h1>Second Brain</h1>
<h2>Windows workers</h2>{"".join(workers) or '<section class="bad">No Windows worker added</section>'}
<h2>Synced records</h2><section>{records}</section>
<h2>Speaker commands</h2><section>{f'<span class="bad">oldest waiting for Windows since {waiting}</span>' if waiting else 'none waiting'}</section>
<h2>Phones</h2>{phone_rows}
<p class="muted">Updated {now:%Y-%m-%d %H:%M} UTC · refreshes every 30 s</p></body></html>"""
