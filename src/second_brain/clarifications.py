"""Human completion of incomplete knowledge, through the private review mailbox."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .io import atomic_write, write_json

NOTE_LOCK = threading.RLock()


class Clarifications:
    def __init__(self, data: Path, review: Path, vault: Path):
        self.data, self.review, self.vault = data, review, vault.resolve()
        data.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY, path TEXT NOT NULL, original TEXT NOT NULL,
                text TEXT NOT NULL, questions TEXT NOT NULL, context TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', answer TEXT, created REAL NOT NULL)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.data / 'clarifications.sqlite3', timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, eid, path, bullet, questions, context):
        relative = path.resolve().relative_to(self.vault).as_posix()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO items(id,path,original,text,questions,context,created) VALUES(?,?,?,?,?,?,?)",
                       (eid, relative, f'- {bullet} <!-- router:{eid} -->', bullet,
                        json.dumps(questions), context[:16000], time.time()))

    def count(self):
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM items WHERE state='pending'").fetchone()[0]

    def catalog(self):
        with self.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT id,path,text,questions,context FROM items WHERE state='pending' ORDER BY created")]
        for row in rows:
            row['questions'] = json.loads(row['questions'])
        write_json(self.review / 'clarifications.json', dict(heartbeat=time.time(), items=rows))

    def command(self, request):
        key, eid, answer = request['id'], request.get('observation'), request.get('name')
        if request.get('action') not in ('clarify', 'clarify_dismiss') or not isinstance(eid, str) or not re.fullmatch('[a-f0-9]{16}', eid):
            raise ValueError('Invalid clarification')
        if request['action'] == 'clarify_dismiss':
            with self.connect() as db:
                db.execute("UPDATE items SET state='dismissed' WHERE id=? AND state='pending'", (eid,))
            self.catalog()
            return dict(id=key, ok=True)
        if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 4000 or any(ord(c) < 32 for c in answer) or '<!--' in answer:
            return dict(id=key, ok=False, error='Enter a single corrected sentence, up to 4000 characters.')
        answer = answer.strip()
        with NOTE_LOCK, self.connect() as db:
            row = db.execute('SELECT * FROM items WHERE id=?', (eid,)).fetchone()
            if row is None:
                return dict(id=key, ok=False, error='This clarification no longer exists.')
            if row['state'] != 'pending':
                return dict(id=key, ok=row['answer'] == answer, error='This item was already resolved.')
            if answer == row['text']:
                return dict(id=key, ok=False, error='Add the missing context before saving.')
            path = (self.vault / row['path']).resolve()
            path.relative_to(self.vault)
            try:
                original = path.read_bytes()
            except FileNotFoundError:
                return dict(id=key, ok=False, error='The note was moved or removed. Keep it as is to dismiss this review.')
            text = original.decode('utf-8')
            new = f'- {answer} <!-- router:{eid} -->'
            lines = text.splitlines(keepends=True)
            matches = [i for i, line in enumerate(lines) if f'<!-- router:{eid} -->' in line]
            if len(matches) != 1 or lines[matches[0]].rstrip('\r\n') not in (row['original'], new):
                return dict(id=key, ok=False, error='The Obsidian entry was edited or removed. Your edits were preserved; resolve this entry in Obsidian.')
            i = matches[0]
            if lines[i].rstrip('\r\n') != new:
                backup = self.data / 'clarification-backups' / f'{eid}.md'
                if not backup.exists():
                    atomic_write(backup, original)
                ending = '\r\n' if lines[i].endswith('\r\n') else '\n' if lines[i].endswith('\n') else ''
                lines[i] = new + ending
                if path.read_bytes() != original:
                    return dict(id=key, ok=False, error='The note changed while saving. Try again.')
                atomic_write(path, ''.join(lines).encode('utf-8'))
            # Replaying after a crash between note publication and this commit is safe.
            db.execute("UPDATE items SET state='resolved', answer=? WHERE id=?", (answer, eid))
        self.catalog()
        return dict(id=key, ok=True)
