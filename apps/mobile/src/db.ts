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
  if (version < 3) {
    await db.execAsync(`
      CREATE TABLE server_commands (id TEXT PRIMARY KEY NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL);
      PRAGMA user_version = 3;
    `);
  }
  if (version < 4) {
    await db.execAsync(`
      CREATE TABLE server_memories (id TEXT PRIMARY KEY NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL);
      PRAGMA user_version = 4;
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

// --- Speaker and clarification commands, applied later by the Windows worker ---

export type CommandPayload =
  | { action: 'assign'; observation: string; person?: string; name?: string; base_hash: string }
  | { action: 'clear' | 'discard'; observation: string; base_hash: string }
  | { action: 'rename'; person: string; name: string; base_hash: string }
  | { action: 'forget'; person: string; base_hash: string }
  | { action: 'clarify'; item: string; answer: string }
  | { action: 'clarify_dismiss'; item: string };

export type ServerCommand = { id: string; payload: CommandPayload; state: 'pending_worker' | 'applied_worker' | 'failed_worker';
  error: string | null; revision: number; updated_at: string };

// queued: not yet accepted by the server. rejected: the server refused it (outbox problem).
export type Command = { id: string; payload: CommandPayload; state: ServerCommand['state'] | 'queued' | 'rejected'; error: string | null;
  seq: number | null };

export async function upsertServerCommand(db: Queries, c: ServerCommand) {
  await db.runAsync(
    `INSERT INTO server_commands (id, revision, data) VALUES (?, ?, ?)
     ON CONFLICT (id) DO UPDATE SET revision = excluded.revision, data = excluded.data WHERE excluded.revision > server_commands.revision`,
    c.id, c.revision, JSON.stringify(c),
  );
}

export async function queueCommand(db: Db, payload: CommandPayload, newId: () => string) {
  await db.runAsync(
    'INSERT INTO outbox (operation_id, entity_type, entity_id, action, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)',
    newId(), 'command', newId(), 'create', JSON.stringify(payload), new Date().toISOString(),
  );
}

// Newest first. A command leaves the outbox once the server accepts it and continues as a server command.
export async function listCommands(db: Db): Promise<Command[]> {
  const queued = await db.getAllAsync<OutboxRow>("SELECT * FROM outbox WHERE entity_type = 'command' ORDER BY seq DESC");
  const saved = await db.getAllAsync<{ data: string }>('SELECT data FROM server_commands');
  return [
    ...queued.map((o): Command => ({ id: o.entity_id, payload: JSON.parse(o.payload), seq: o.seq,
      state: o.status === 'pending' ? 'queued' : 'rejected', error: o.error })),
    ...saved.map((r) => JSON.parse(r.data) as ServerCommand)
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
      .map((c): Command => ({ id: c.id, payload: c.payload, state: c.state, error: c.error, seq: null })),
  ];
}

// --- Memories: created by the Windows extraction, curated here ---

export type MemoryState = 'proposed' | 'confirmed' | 'archived';
export type ServerMemory = {
  id: string; kind: 'fact' | 'decision' | 'idea'; text: string; state: MemoryState; revision: number;
  original_text: string; confidence: number | null; entity_type: string | null; entity_name: string | null;
  observed_at: string | null; updated_at: string; deleted_at: string | null;
  evidence: { source_id: string | null; note: string | null; recorded_at: string | null; excerpt?: string; start?: number; end?: number } | null;
};
export type Memory = ServerMemory & { pending: number; problem: (Omit<Problem, 'server'> & { server: ServerMemory | null }) | null };
export type MemoryOp = { action: 'set_text'; payload: { text: string } } | { action: 'set_state'; payload: { state: MemoryState } };

export async function upsertServerMemory(db: Queries, m: ServerMemory) {
  await db.runAsync(
    `INSERT INTO server_memories (id, revision, data) VALUES (?, ?, ?)
     ON CONFLICT (id) DO UPDATE SET revision = excluded.revision, data = excluded.data WHERE excluded.revision > server_memories.revision`,
    m.id, m.revision, JSON.stringify(m),
  );
}

export async function applyMemory(db: Db, id: string, op: MemoryOp, newId: () => string) {
  await db.withExclusiveTransactionAsync(async (txn) => {
    if (!(await txn.getFirstAsync('SELECT 1 FROM server_memories WHERE id = ?', id))) throw new Error(`memory ${id} not found`);
    await txn.runAsync(
      'INSERT INTO outbox (operation_id, entity_type, entity_id, action, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)',
      newId(), 'memory', id, op.action, JSON.stringify(op.payload), new Date().toISOString(),
    );
  });
}

// Local search over the cache with queued edits applied: never touches the network or the vault.
// The query matches text, the original extraction, the person/project name and the date (so
// "2026-10" or "Alex" filter by month or person).
// ponytail: in-memory scan, fine for a few thousand memories; switch to FTS5 beyond that.
export async function listMemories(db: Db, filter: { query?: string; state?: MemoryState } = {}): Promise<Memory[]> {
  const rows = await db.getAllAsync<{ data: string }>('SELECT data FROM server_memories');
  const ops = await db.getAllAsync<OutboxRow>("SELECT * FROM outbox WHERE entity_type = 'memory' ORDER BY seq");
  const view = new Map<string, Memory>(rows.map((r) => {
    const m = JSON.parse(r.data) as ServerMemory;
    return [m.id, { ...m, pending: 0, problem: null }];
  }));
  for (const op of ops) {
    const m = view.get(op.entity_id);
    if (!m) continue;
    const payload = JSON.parse(op.payload);
    if (op.action === 'set_text') m.text = payload.text;
    if (op.action === 'set_state') m.state = payload.state;
    m.pending += 1;
    if (op.status !== 'pending' && !m.problem) {
      m.problem = { seq: op.seq, outcome: op.status, error: op.error ?? '', server: op.server_entity ? JSON.parse(op.server_entity) : null };
    }
  }
  const words = (filter.query ?? '').toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return [...view.values()]
    .filter((m) => !m.deleted_at && (!filter.state || m.state === filter.state))
    .filter((m) => {
      const hay = [m.text, m.original_text, m.entity_name, m.observed_at, m.kind].join(' ').toLocaleLowerCase();
      return words.every((w) => hay.includes(w));
    })
    .sort((a, b) => (b.observed_at ?? '').localeCompare(a.observed_at ?? '') || a.id.localeCompare(b.id));
}

type OutboxRow = { seq: number; entity_id: string; action: Op['action'] | MemoryOp['action']; payload: string; created_at: string;
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
