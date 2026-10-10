// Local cache (docs/mobile/architecture-and-sync.md, sync invariants 1 and 5).
// server_todos holds only server-acknowledged state; outbox holds unacknowledged
// edits. The UI shows server state with the outbox replayed on top, so an
// acknowledgment or a pulled change rebases unsent edits without rewriting them.
import type { SQLiteDatabase } from 'expo-sqlite';

export type TodoState = 'open' | 'completed' | 'dismissed';

export type ServerTodo = {
  id: string;
  title: string;
  state: TodoState;
  revision: number;
  updated_at: string;
  deleted_at: string | null;
};

export type Todo = {
  id: string;
  title: string;
  state: TodoState;
  pending: number; // unacknowledged operations, including a stuck one
  problem: Problem | null;
};

// A conflicted or rejected operation blocks later edits to its entity until resolved.
export type Problem = { seq: number; outcome: 'conflict' | 'rejected'; error: string; server: ServerTodo | null };

export type Op =
  | { action: 'create'; payload: { title: string } }
  | { action: 'set_title'; payload: { title: string } }
  | { action: 'set_state'; payload: { state: TodoState } };

type Queries = Pick<SQLiteDatabase, 'runAsync' | 'getFirstAsync' | 'getAllAsync'>;
// Only the methods used here, so tests can pass a node:sqlite adapter.
export type Db = Queries & Pick<SQLiteDatabase, 'execAsync'> & {
  withExclusiveTransactionAsync(task: (txn: Queries) => Promise<void>): Promise<void>;
};

export async function migrate(db: Db) {
  const row = await db.getFirstAsync<{ user_version: number }>('PRAGMA user_version');
  const version = row?.user_version ?? 0;
  if (version < 2) {
    // v1 (spike) held only synthetic data and had no server state, so it is replaced.
    await db.execAsync(`
      PRAGMA journal_mode = WAL;
      DROP TABLE IF EXISTS todos;
      DROP TABLE IF EXISTS outbox;
      CREATE TABLE server_todos (
        id TEXT PRIMARY KEY NOT NULL,
        title TEXT NOT NULL,
        state TEXT NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        deleted_at TEXT
      );
      CREATE TABLE outbox (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        action TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL,
        -- Fixed on first send so a retry repeats the identical request (server receipts).
        sent_base_revision INTEGER,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'conflict', 'rejected')),
        error TEXT,
        server_entity TEXT
      );
      CREATE INDEX outbox_entity ON outbox (entity_id, seq);
      CREATE TABLE sync_meta (key TEXT PRIMARY KEY NOT NULL, value TEXT);
      PRAGMA user_version = 2;
    `);
  }
}

export async function getMeta(db: Queries, key: string) {
  return (await db.getFirstAsync<{ value: string | null }>('SELECT value FROM sync_meta WHERE key = ?', key))?.value ?? null;
}

export async function setMeta(db: Queries, key: string, value: string | number | null) {
  await db.runAsync(
    'INSERT INTO sync_meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value',
    key, value === null ? null : String(value),
  );
}

// Keeps the newest revision; pulls and acknowledgments may arrive in either order.
export async function upsertServerTodo(db: Queries, t: ServerTodo) {
  await db.runAsync(
    `INSERT INTO server_todos (id, title, state, revision, updated_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?)
     ON CONFLICT (id) DO UPDATE SET title = excluded.title, state = excluded.state, revision = excluded.revision,
       updated_at = excluded.updated_at, deleted_at = excluded.deleted_at
     WHERE excluded.revision > server_todos.revision`,
    t.id, t.title, t.state, t.revision, t.updated_at, t.deleted_at,
  );
}

type OutboxRow = { seq: number; entity_id: string; action: Op['action']; payload: string; created_at: string;
  status: 'pending' | Problem['outcome']; error: string | null; server_entity: string | null };

export async function listTodos(db: Db): Promise<Todo[]> {
  const base = await db.getAllAsync<ServerTodo>('SELECT * FROM server_todos WHERE deleted_at IS NULL');
  const ops = await db.getAllAsync<OutboxRow>("SELECT * FROM outbox WHERE entity_type = 'todo' ORDER BY seq");
  const view = new Map<string, Todo & { touched: string }>(
    base.map((t) => [t.id, { id: t.id, title: t.title, state: t.state, pending: 0, problem: null, touched: t.updated_at }]),
  );
  for (const op of ops) {
    const payload = JSON.parse(op.payload);
    if (op.action === 'create' && !view.has(op.entity_id)) {
      view.set(op.entity_id, { id: op.entity_id, title: payload.title, state: 'open', pending: 0, problem: null, touched: op.created_at });
    }
    const todo = view.get(op.entity_id);
    if (!todo) continue; // removed on the server; its op shows up as rejected
    if (op.action === 'set_title') todo.title = payload.title;
    if (op.action === 'set_state') todo.state = payload.state;
    todo.pending += 1;
    if (op.created_at > todo.touched) todo.touched = op.created_at;
    if (op.status !== 'pending' && !todo.problem) {
      todo.problem = { seq: op.seq, outcome: op.status, error: op.error ?? '', server: op.server_entity ? JSON.parse(op.server_entity) : null };
    }
  }
  return [...view.values()]
    .filter((t) => t.state !== 'dismissed' || t.problem)
    .sort((a, b) => Number(a.state === 'completed') - Number(b.state === 'completed') || b.touched.localeCompare(a.touched))
    .map(({ touched, ...t }) => t);
}

// Desired-state operations only: completion is set_state, never a toggle.
export async function applyLocal(db: Db, entityId: string, op: Op, newId: () => string) {
  await db.withExclusiveTransactionAsync(async (txn) => {
    if (op.action !== 'create') {
      const known = await txn.getFirstAsync(
        "SELECT 1 FROM server_todos WHERE id = ? UNION SELECT 1 FROM outbox WHERE entity_id = ? AND action = 'create'",
        entityId, entityId,
      );
      if (!known) throw new Error(`todo ${entityId} not found`);
    }
    await txn.runAsync(
      'INSERT INTO outbox (operation_id, entity_type, entity_id, action, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)',
      newId(), 'todo', entityId, op.action, JSON.stringify(op.payload), new Date().toISOString(),
    );
  });
}
