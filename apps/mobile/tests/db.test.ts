// Local cache and replay: `npm test` (Node >= 22.6).
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { applyLocal, applyMemory, exclusive, listCommands, listMemories, listTodos, migrate, queueCommand, upsertServerCommand, upsertServerMemory,
  upsertServerTodo } from '../src/db.ts';
import { openDb } from './sqlite.ts';

const path = `${process.env.TMPDIR ?? '/tmp'}/sb-db-test-${randomUUID()}.db`;
let db = openDb(path);
await migrate(db);
await migrate(db); // idempotent

const id = randomUUID();
await applyLocal(db, id, { action: 'create', payload: { title: 'Buy milk' } }, randomUUID);
await applyLocal(db, id, { action: 'set_state', payload: { state: 'completed' } }, randomUUID);

// Reopen the file: queued edits survive an app kill and still render.
db = openDb(path);
let [todo] = await listTodos(db);
assert.deepEqual([todo.title, todo.state, todo.pending], ['Buy milk', 'completed', 2]);

// A failed edit rolls back and queues nothing.
await assert.rejects(applyLocal(db, randomUUID(), { action: 'set_title', payload: { title: 'x' } }, randomUUID));
assert.equal((await db.getFirstAsync<{ n: number }>('SELECT COUNT(*) AS n FROM outbox'))!.n, 2);

// Server state underneath: an unsent local edit still wins in the view (rebase).
const other = randomUUID();
await upsertServerTodo(db, { id: other, title: 'Server title', state: 'open', revision: 3, updated_at: '2026-01-01T00:00:00Z', deleted_at: null });
await applyLocal(db, other, { action: 'set_title', payload: { title: 'Local title' } }, randomUUID);
await upsertServerTodo(db, { id: other, title: 'Newer server title', state: 'completed', revision: 4, updated_at: '2026-01-02T00:00:00Z', deleted_at: null });
todo = (await listTodos(db)).find((t) => t.id === other)!;
assert.deepEqual([todo.title, todo.state], ['Local title', 'completed']);

// An older revision never overwrites a newer one.
await upsertServerTodo(db, { id: other, title: 'Stale', state: 'open', revision: 2, updated_at: '2026-01-03T00:00:00Z', deleted_at: null });
const row = await db.getFirstAsync<{ title: string }>('SELECT title FROM server_todos WHERE id = ?', other);
assert.equal(row!.title, 'Newer server title');

// Dismissed items leave the list but keep their queued operation.
await applyLocal(db, id, { action: 'set_state', payload: { state: 'dismissed' } }, randomUUID);
assert.ok(!(await listTodos(db)).some((t) => t.id === id));

// Commands: queued locally, then the server's state wins and older revisions never regress it.
await queueCommand(db, { action: 'clarify_dismiss', item: 'abc' }, randomUUID);
assert.deepEqual((await listCommands(db)).map((c) => c.state), ['queued']);
const command = { id: randomUUID(), payload: { action: 'forget' as const, person: 'p', base_hash: 'h' }, error: null };
await upsertServerCommand(db, { ...command, state: 'applied_worker', revision: 2, updated_at: '2026-01-02T00:00:00Z' });
await upsertServerCommand(db, { ...command, state: 'pending_worker', revision: 1, updated_at: '2026-01-01T00:00:00Z' });
assert.deepEqual((await listCommands(db)).map((c) => c.state), ['queued', 'applied_worker']);
assert.equal((await listTodos(db)).some((t) => t.title === undefined), false); // commands never appear as todos

// A sync page being written while the user archives a memory: on the phone each write transaction
// is its own connection, so overlapping ones failed with "database is locked". They now queue.
const memory = { id: randomUUID(), kind: 'fact' as const, text: 'Likes tea', state: 'proposed' as const, revision: 1,
  original_text: 'Likes tea', confidence: null, entity_type: null, entity_name: null, observed_at: null,
  updated_at: '2026-01-01T00:00:00Z', deleted_at: null, evidence: null };
await upsertServerMemory(db, memory);
let releasePage!: () => void;
const pageWriting = new Promise<void>((r) => (releasePage = r));
const pull = exclusive(db, async (txn) => {
  await upsertServerMemory(txn, { ...memory, id: randomUUID() });
  await pageWriting; // the rest of the page is still being written
});
const archive = applyMemory(db, memory.id, { action: 'set_state', payload: { state: 'archived' } }, randomUUID);
setTimeout(releasePage, 50);
await Promise.all([pull, archive]);
assert.deepEqual((await listMemories(db, { state: 'archived' })).map((m) => m.id), [memory.id]);

// Open todos in the order they came up, oldest first; done ones most recently finished first.
const order = openDb(`${process.env.TMPDIR ?? '/tmp'}/sb-order-${randomUUID()}.db`);
await migrate(order);
const at = (day: number) => `2026-10-${String(day).padStart(2, '0')}T12:00:00Z`;
for (const [title, state, created, updated] of [
  ['middle', 'open', 5, 20], ['oldest', 'open', 2, 21], ['newest', 'open', 9, 9], ['done early', 'completed', 1, 10], ['done late', 'completed', 3, 15],
] as const) {
  await upsertServerTodo(order, { id: randomUUID(), title, state, revision: 1, created_at: at(created), updated_at: at(updated), deleted_at: null });
}
await applyLocal(order, randomUUID(), { action: 'create', payload: { title: 'added now' } }, randomUUID);
assert.deepEqual((await listTodos(order)).map((t) => t.title), ['oldest', 'middle', 'newest', 'added now', 'done late', 'done early']);

console.log('db.test.ts: ok');
