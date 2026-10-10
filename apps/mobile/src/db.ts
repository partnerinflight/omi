// Local todo cache plus operation outbox (sync invariant 1 in
// docs/mobile/architecture-and-sync.md): every edit writes the entity overlay
// and its outbox row in one transaction. Nothing here talks to a server yet.
import type { SQLiteDatabase } from 'expo-sqlite';

export type TodoState = 'open' | 'completed' | 'dismissed';

export type Todo = {
  id: string;
  title: string;
  state: TodoState;
  revision: number;
  pending: number; // queued operations not yet acknowledged
};

// Only the methods used below, so tests can pass a node:sqlite adapter.
export type Db = Pick<SQLiteDatabase, 'execAsync' | 'getFirstAsync' | 'getAllAsync'> & {
  withExclusiveTransactionAsync(task: (txn: Pick<SQLiteDatabase, 'runAsync' | 'getFirstAsync'>) => Promise<void>): Promise<void>;
};

export async function migrate(db: Db) {
  const row = await db.getFirstAsync<{ user_version: number }>('PRAGMA user_version');
  if ((row?.user_version ?? 0) < 1) {
    await db.execAsync(`
      PRAGMA journal_mode = WAL;
      CREATE TABLE todos (
        id TEXT PRIMARY KEY NOT NULL,
        title TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('open', 'completed', 'dismissed')),
        revision INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
      );
      CREATE TABLE outbox (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        base_revision INTEGER NOT NULL,
        action TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
      );
      PRAGMA user_version = 1;
    `);
  }
}

export function listTodos(db: Db) {
  return db.getAllAsync<Todo>(`
    SELECT t.id, t.title, t.state, t.revision,
           (SELECT COUNT(*) FROM outbox o WHERE o.entity_id = t.id) AS pending
    FROM todos t
    WHERE t.state != 'dismissed'
    ORDER BY t.state = 'completed', t.updated_at DESC`);
}

type Op =
  | { action: 'create'; payload: { title: string } }
  | { action: 'set_title'; payload: { title: string } }
  | { action: 'set_state'; payload: { state: TodoState } };

// Desired-state operations only: completion is set_state, never a toggle.
export async function applyLocal(db: Db, entityId: string, op: Op, newId: () => string) {
  const now = new Date().toISOString();
  await db.withExclusiveTransactionAsync(async (txn) => {
    if (op.action === 'create') {
      await txn.runAsync('INSERT INTO todos (id, title, state, updated_at) VALUES (?, ?, ?, ?)', entityId, op.payload.title, 'open', now);
    } else if (op.action === 'set_title') {
      await txn.runAsync('UPDATE todos SET title = ?, updated_at = ? WHERE id = ?', op.payload.title, now, entityId);
    } else {
      await txn.runAsync('UPDATE todos SET state = ?, updated_at = ? WHERE id = ?', op.payload.state, now, entityId);
    }
    const row = await txn.getFirstAsync<{ revision: number }>('SELECT revision FROM todos WHERE id = ?', entityId);
    if (!row) throw new Error(`todo ${entityId} not found`);
    await txn.runAsync(
      'INSERT INTO outbox (operation_id, entity_type, entity_id, base_revision, action, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
      newId(), 'todo', entityId, row.revision, op.action, JSON.stringify(op.payload), now,
    );
  });
}
