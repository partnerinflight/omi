// Runs src/db.ts against node:sqlite: `npm test` (Node >= 22.6).
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { applyLocal, listTodos, migrate, type Db } from '../src/db.ts';

function adapter(raw: DatabaseSync): Db {
  const runAsync = async (sql: string, ...args: any[]) => {
    const r = raw.prepare(sql).run(...args);
    return { lastInsertRowId: Number(r.lastInsertRowid), changes: Number(r.changes) };
  };
  const getFirstAsync = async (sql: string, ...args: any[]) => (raw.prepare(sql).get(...args) ?? null) as any;
  return {
    execAsync: async (sql: string) => raw.exec(sql),
    getFirstAsync,
    getAllAsync: async (sql: string, ...args: any[]) => raw.prepare(sql).all(...args) as any,
    withExclusiveTransactionAsync: async (task) => {
      raw.exec('BEGIN EXCLUSIVE');
      try {
        await task({ runAsync, getFirstAsync } as any);
        raw.exec('COMMIT');
      } catch (e) {
        raw.exec('ROLLBACK');
        throw e;
      }
    },
  } as Db;
}

const path = `${process.env.TMPDIR ?? '/tmp'}/sb-db-test-${randomUUID()}.db`;
let db = adapter(new DatabaseSync(path));
await migrate(db);
await migrate(db); // idempotent

const id = randomUUID();
await applyLocal(db, id, { action: 'create', payload: { title: 'Buy milk' } }, randomUUID);
await applyLocal(db, id, { action: 'set_state', payload: { state: 'completed' } }, randomUUID);

// Reopen the file: edits and queued operations survive an app kill.
db = adapter(new DatabaseSync(path));
const [todo] = await listTodos(db);
assert.equal(todo.state, 'completed');
assert.equal(todo.pending, 2);
const ops = await db.getAllAsync<{ action: string }>('SELECT action FROM outbox ORDER BY seq');
assert.deepEqual(ops.map((o) => o.action), ['create', 'set_state']);

// A failed edit rolls back the overlay and writes no outbox row.
await assert.rejects(applyLocal(db, 'missing', { action: 'set_title', payload: { title: 'x' } }, randomUUID));
assert.equal((await db.getFirstAsync<{ n: number }>('SELECT COUNT(*) AS n FROM outbox'))!.n, 2);

// Dismissed items leave the list but keep their queued operation.
await applyLocal(db, id, { action: 'set_state', payload: { state: 'dismissed' } }, randomUUID);
assert.equal((await listTodos(db)).length, 0);
assert.equal((await db.getFirstAsync<{ n: number }>('SELECT COUNT(*) AS n FROM outbox'))!.n, 3);

console.log('db.test.ts: ok');
