// Phone side of the sync contract (docs/mobile/architecture-and-sync.md):
// push the first queued operation per entity, then pull ordered changes.
// Foreground only: callers trigger it on open/resume, after edits, on a timer
// and on manual refresh. Background execution is never required for correctness.
import { getMeta, setMeta, upsertServerTodo, type Db, type ServerTodo } from './db.ts';

export type Tokens = { access_token: string; refresh_token: string };

export interface TokenStore {
  get(): Promise<Tokens | null>;
  set(tokens: Tokens): Promise<void>;
  clear(): Promise<void>;
}

export type SyncState = 'unpaired' | 'synced' | 'offline' | 'auth_required' | 'error';

export type SyncStatus = {
  state: SyncState;
  pending: number;
  problems: number;
  lastSuccessAt: string | null;
  error: string | null;
};

type Options = {
  fetch?: typeof fetch;
  newId: () => string;
  random?: () => number;
  now?: () => number;
  allowHttpLocalhost?: boolean; // development only; production requires verified HTTPS
};

class AuthRequired extends Error {}
class HttpError extends Error {
  status: number;
  retryAfter?: number;
  constructor(status: number, retryAfter?: number) {
    super(`HTTP ${status}`);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

const TIMEOUT_MS = 15_000;
const MAX_BACKOFF_MS = 5 * 60_000;
const PUSH_BATCH = 100;
const PULL_PAGE = 500;

export function serverUrlProblem(url: string, allowHttpLocalhost = false): string | null {
  if (/^https:\/\/[^/\s]+$/.test(url)) return null;
  if (allowHttpLocalhost && /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(url)) return null;
  return 'Use an https:// address with no path.';
}

export function createSync(db: Db, tokens: TokenStore, opts: Options) {
  const http = opts.fetch ?? fetch;
  const now = opts.now ?? Date.now;
  const random = opts.random ?? Math.random;
  let running: Promise<SyncStatus> | null = null;
  let again = false;

  async function request(url: string, init: RequestInit = {}) {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), TIMEOUT_MS);
    try {
      return await http(url, { ...init, signal: abort.signal, headers: { 'Content-Type': 'application/json', ...init.headers } });
    } finally {
      clearTimeout(timer);
    }
  }

  async function serverUrl() {
    const url = await getMeta(db, 'server_url');
    if (!url) throw new AuthRequired('not paired');
    return url;
  }

  async function refresh(url: string, current: Tokens) {
    // ponytail: a lost refresh response leaves a used token, and reusing it revokes
    // the device (theft detection), so the phone must pair again. Add a server-side
    // grace window if that happens in practice.
    const res = await request(`${url}/v1/auth/refresh`, { method: 'POST', body: JSON.stringify({ refresh_token: current.refresh_token }) });
    if (res.status === 401) throw new AuthRequired('refresh rejected');
    if (!res.ok) throw new HttpError(res.status);
    const next = await res.json();
    await tokens.set({ access_token: next.access_token, refresh_token: next.refresh_token });
  }

  async function api(path: string, init: RequestInit = {}, retried = false): Promise<Response> {
    const url = await serverUrl();
    const current = await tokens.get();
    if (!current) throw new AuthRequired('no credentials');
    const res = await request(url + path, { ...init, headers: { Authorization: `Bearer ${current.access_token}` } });
    if (res.status === 401 && !retried) {
      await refresh(url, current);
      return api(path, init, true);
    }
    if (res.status === 401) throw new AuthRequired('access rejected');
    if (res.status === 429 || res.status >= 500) {
      throw new HttpError(res.status, Number(res.headers.get('Retry-After')) || undefined);
    }
    return res;
  }

  async function push() {
    for (;;) {
      // The oldest operation per entity, unless it is stuck: later edits wait behind it.
      const ops = await db.getAllAsync<{ seq: number; operation_id: string; entity_type: string; entity_id: string;
        action: string; payload: string; sent_base_revision: number | null }>(
        `SELECT o.* FROM outbox o
         WHERE o.status = 'pending' AND o.seq = (SELECT MIN(seq) FROM outbox p WHERE p.entity_id = o.entity_id)
         ORDER BY o.seq LIMIT ?`, PUSH_BATCH,
      );
      if (ops.length === 0) return;

      await db.withExclusiveTransactionAsync(async (txn) => {
        for (const op of ops) {
          if (op.sent_base_revision !== null) continue;
          const row = await txn.getFirstAsync<{ revision: number }>('SELECT revision FROM server_todos WHERE id = ?', op.entity_id);
          op.sent_base_revision = op.action === 'create' ? 0 : row?.revision ?? 0;
          await txn.runAsync('UPDATE outbox SET sent_base_revision = ? WHERE seq = ?', op.sent_base_revision, op.seq);
        }
      });

      const res = await api('/v1/operations', {
        method: 'POST',
        body: JSON.stringify({ operations: ops.map((op) => ({ operation_id: op.operation_id, entity_type: op.entity_type,
          entity_id: op.entity_id, base_revision: op.sent_base_revision, action: op.action, payload: JSON.parse(op.payload) })) }),
      });
      if (!res.ok) throw new HttpError(res.status);
      const { results } = (await res.json()) as { results: { operation_id: string; outcome: string; error: string | null; entity: ServerTodo | null }[] };

      await db.withExclusiveTransactionAsync(async (txn) => {
        for (const r of results) {
          if (r.entity) await upsertServerTodo(txn, r.entity);
          if (r.outcome === 'applied') {
            await txn.runAsync('DELETE FROM outbox WHERE operation_id = ?', r.operation_id);
          } else {
            await txn.runAsync('UPDATE outbox SET status = ?, error = ?, server_entity = ? WHERE operation_id = ?',
              r.outcome === 'conflict' ? 'conflict' : 'rejected', r.error, r.entity ? JSON.stringify(r.entity) : null, r.operation_id);
          }
        }
      });
    }
  }

  async function bootstrap() {
    const res = await api('/v1/bootstrap');
    if (!res.ok) throw new HttpError(res.status);
    const snapshot = (await res.json()) as { cursor: string; todos: ServerTodo[] };
    // Replaces only acknowledged state, atomically; the outbox is untouched.
    await db.withExclusiveTransactionAsync(async (txn) => {
      await txn.runAsync('DELETE FROM server_todos');
      for (const t of snapshot.todos) await upsertServerTodo(txn, t);
      await setMeta(txn, 'cursor', snapshot.cursor);
    });
  }

  async function pull() {
    for (;;) {
      const cursor = await getMeta(db, 'cursor');
      if (!cursor) return bootstrap();
      const res = await api(`/v1/changes?cursor=${encodeURIComponent(cursor)}&limit=${PULL_PAGE}`);
      if (res.status === 410) {
        await setMeta(db, 'cursor', null); // server restored or epoch changed
        return bootstrap();
      }
      if (!res.ok) throw new HttpError(res.status);
      const page = (await res.json()) as { cursor: string; has_more: boolean; changes: { entity_type: string; entity: ServerTodo }[] };
      await db.withExclusiveTransactionAsync(async (txn) => {
        for (const c of page.changes) if (c.entity_type === 'todo') await upsertServerTodo(txn, c.entity);
        await setMeta(txn, 'cursor', page.cursor);
      });
      if (!page.has_more) return;
    }
  }

  async function status(): Promise<SyncStatus> {
    const counts = await db.getFirstAsync<{ pending: number; problems: number }>(
      "SELECT COUNT(*) AS pending, COALESCE(SUM(status != 'pending'), 0) AS problems FROM outbox",
    );
    const paired = (await getMeta(db, 'server_url')) !== null;
    return {
      state: paired ? ((await getMeta(db, 'state')) as SyncState | null) ?? 'offline' : 'unpaired',
      pending: counts?.pending ?? 0,
      problems: counts?.problems ?? 0,
      lastSuccessAt: await getMeta(db, 'last_success_at'),
      error: await getMeta(db, 'last_error'),
    };
  }

  async function finish(state: SyncState, error: string | null = null) {
    await setMeta(db, 'state', state);
    await setMeta(db, 'last_error', error);
    return status();
  }

  async function run(force: boolean): Promise<SyncStatus> {
    const state = await getMeta(db, 'state');
    if (!(await getMeta(db, 'server_url')) || state === 'auth_required') return status();
    const nextAttempt = Number(await getMeta(db, 'next_attempt_at') ?? 0);
    if (!force && now() < nextAttempt) return status(); // still backing off
    try {
      await push();
      await pull();
      await setMeta(db, 'failures', 0);
      await setMeta(db, 'next_attempt_at', null);
      await setMeta(db, 'last_success_at', new Date(now()).toISOString());
      return finish('synced');
    } catch (e) {
      if (e instanceof AuthRequired) {
        // Do not spin: keep every edit and wait for the user to pair again.
        return finish('auth_required');
      }
      const failures = Number(await getMeta(db, 'failures') ?? 0) + 1;
      const backoff = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** failures) * (0.5 + random() / 2);
      const wait = e instanceof HttpError && e.retryAfter ? Math.max(e.retryAfter * 1000, backoff) : backoff;
      await setMeta(db, 'failures', failures);
      await setMeta(db, 'next_attempt_at', now() + wait);
      // Network failures and timeouts are offline; server responses are errors.
      return e instanceof HttpError ? finish('error', e.message) : finish('offline');
    }
  }

  return {
    status,

    // Single flight. A trigger during a run (say, an edit made mid-pull) causes one
    // more run afterwards, so the edit is not left waiting for the next timer tick.
    sync(force = false) {
      if (running) {
        again = true;
        return running;
      }
      running = (async () => {
        let result: SyncStatus;
        do {
          again = false;
          result = await run(force);
        } while (again && result.state === 'synced');
        return result;
      })().finally(() => (running = null));
      return running;
    },

    async pair(url: string, code: string) {
      url = url.trim().replace(/\/+$/, '');
      const problem = serverUrlProblem(url, opts.allowHttpLocalhost);
      if (problem) throw new Error(problem);
      const existing = await getMeta(db, 'server_url');
      // Caches and queued edits are bound to one server (invariant 8).
      if (existing && existing !== url) throw new Error('Sign out of the current server first.');
      const res = await request(`${url}/v1/pair`, { method: 'POST', body: JSON.stringify({ code }) });
      if (res.status === 401) throw new Error('That code is invalid or expired.');
      if (res.status === 429) throw new Error('Too many attempts. Try again later.');
      if (!res.ok) throw new Error(`Pairing failed (HTTP ${res.status}).`);
      const body = await res.json();
      await tokens.set({ access_token: body.access_token, refresh_token: body.refresh_token });
      await db.withExclusiveTransactionAsync(async (txn) => {
        await setMeta(txn, 'server_url', url);
        await setMeta(txn, 'device_id', body.device_id);
        await setMeta(txn, 'state', 'offline'); // until the first successful sync
        await setMeta(txn, 'failures', 0);
        await setMeta(txn, 'next_attempt_at', null);
      });
    },

    async resolve(seq: number, choice: 'keep_mine' | 'discard') {
      if (choice === 'discard') {
        await db.runAsync("DELETE FROM outbox WHERE seq = ? AND status != 'pending'", seq);
        return;
      }
      // A new operation against the latest server revision (invariant 3).
      await db.runAsync(
        `UPDATE outbox SET status = 'pending', error = NULL, server_entity = NULL, sent_base_revision = NULL, operation_id = ?
         WHERE seq = ? AND status = 'conflict' AND action != 'create'`,
        opts.newId(), seq,
      );
    },

    // Without discard, refuses while edits are queued so the UI can warn first.
    async signOut(discard: boolean) {
      const { pending } = await status();
      if (pending > 0 && !discard) return { signedOut: false, pending };
      const current = await tokens.get();
      const url = await getMeta(db, 'server_url');
      if (current && url) {
        await request(`${url}/v1/auth/revoke`, { method: 'POST', headers: { Authorization: `Bearer ${current.access_token}` } })
          .catch(() => undefined); // offline: revocation cannot reach the server, local data is still cleared
      }
      await tokens.clear();
      await db.execAsync('DELETE FROM outbox; DELETE FROM server_todos; DELETE FROM sync_meta;');
      return { signedOut: true, pending: 0 };
    },
  };
}

export type Sync = ReturnType<typeof createSync>;
