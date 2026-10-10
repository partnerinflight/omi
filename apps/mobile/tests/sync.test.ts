// End to end against the real API (../../server) and Postgres 14 in Docker:
// `npm run test:sync`. Requires Docker and uv.
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { createServer } from 'node:net';
import { applyLocal, listTodos, migrate, type Db } from '../src/db.ts';
import { createSync, type Tokens, type TokenStore } from '../src/sync.ts';
import { openDb } from './sqlite.ts';

const serverDir = new URL('../../../server/', import.meta.url).pathname;

function freePort(): Promise<number> {
  return new Promise((resolve) => {
    const s = createServer().listen(0, '127.0.0.1', () => {
      const { port } = s.address() as { port: number };
      s.close(() => resolve(port));
    });
  });
}

const pgPort = await freePort();
const apiPort = await freePort();
const container = `sb-sync-test-${randomUUID().slice(0, 8)}`;
const env = { ...process.env, SB_DATABASE_URL: `postgresql://postgres:test@127.0.0.1:${pgPort}/postgres`, SB_PORT: String(apiPort) };
execFileSync('docker', ['run', '-d', '--rm', '--name', container, '-e', 'POSTGRES_PASSWORD=test', '-p', `127.0.0.1:${pgPort}:5432`, 'postgres:14-alpine'], { stdio: 'ignore' });
const admin = (...args: string[]) => execFileSync('uv', ['run', '-q', 'python', '-m', 'sb_server', ...args], { cwd: serverDir, env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
let server: ReturnType<typeof spawn> | undefined;

try {
  for (let i = 0; ; i++) {
    try {
      admin('migrate');
      break;
    } catch (e) {
      if (i > 60) throw e;
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  server = spawn('uv', ['run', '-q', 'python', '-m', 'sb_server', 'serve'], { cwd: serverDir, env, stdio: 'ignore' });
  const url = `http://127.0.0.1:${apiPort}`;
  for (let i = 0; ; i++) {
    try {
      if ((await fetch(`${url}/v1/health`)).ok) break;
    } catch {}
    if (i > 100) throw new Error('server did not start');
    await new Promise((r) => setTimeout(r, 200));
  }
  await scenarios(url);
  console.log('sync.test.ts: ok');
} finally {
  server?.kill();
  execFileSync('docker', ['rm', '-f', container], { stdio: 'ignore' });
}

// One simulated phone: a SQLite file, a token store, and a switchable network.
function phone(name: string) {
  const path = `${process.env.TMPDIR ?? '/tmp'}/sb-sync-${name}-${randomUUID()}.db`;
  let saved: Tokens | null = null;
  const tokens: TokenStore = { get: async () => saved, set: async (t) => void (saved = t), clear: async () => void (saved = null) };
  const net = { offline: false, dropNextResponse: false, requests: 0 };
  const fakeFetch: typeof fetch = async (input, init) => {
    if (net.offline) throw new TypeError('Network request failed');
    net.requests++;
    const res = await fetch(input, init);
    if (net.dropNextResponse) {
      net.dropNextResponse = false;
      throw new TypeError('Network connection lost'); // the server did the work; the phone never hears back
    }
    return res;
  };
  const p = {
    db: undefined as unknown as Db,
    sync: undefined as unknown as ReturnType<typeof createSync>,
    net,
    tokens,
    // A fresh process on the same file: what an app kill and relaunch looks like.
    async open() {
      p.db = openDb(path);
      await migrate(p.db);
      p.sync = createSync(p.db, tokens, { fetch: fakeFetch, newId: randomUUID, allowHttpLocalhost: true });
    },
    edit: (id: string, op: Parameters<typeof applyLocal>[2]) => applyLocal(p.db, id, op, randomUUID),
    todos: () => listTodos(p.db),
  };
  return p;
}

async function scenarios(url: string) {
  const code = () => admin('pair', 'test').split(/\s+/)[0];
  const a = phone('a');
  const b = phone('b');
  await a.open();
  await b.open();

  // Pairing validates the address and binds the cache to one server.
  await assert.rejects(a.sync.pair('http://example.com', code()), /https/);
  await assert.rejects(a.sync.pair(url, 'WRONGCODE1'), /invalid or expired/);
  await a.sync.pair(url, code());
  await b.sync.pair(url, code());
  await assert.rejects(a.sync.pair('https://other.example', code()), /Sign out/);

  // Offline edits survive a kill, then sync once on reconnect.
  const milk = randomUUID();
  a.net.offline = true;
  await a.edit(milk, { action: 'create', payload: { title: 'Buy milk' } });
  await a.edit(milk, { action: 'set_state', payload: { state: 'completed' } });
  assert.equal((await a.sync.sync()).state, 'offline');
  await a.open();
  a.net.offline = false;
  let s = await a.sync.sync(true);
  assert.deepEqual([s.state, s.pending, s.problems], ['synced', 0, 0]);
  assert.deepEqual((await a.todos()).map((t) => [t.title, t.state, t.pending]), [['Buy milk', 'completed', 0]]);

  // The second device converges.
  await b.sync.sync(true);
  assert.deepEqual((await b.todos()).map((t) => [t.id, t.state]), [[milk, 'completed']]);

  // Lost response after the server committed: the retry repeats the same
  // operation id and base revision, and the receipt prevents a second apply.
  const bread = randomUUID();
  await a.edit(bread, { action: 'create', payload: { title: 'Bread' } });
  a.net.dropNextResponse = true;
  assert.equal((await a.sync.sync(true)).state, 'offline');
  await a.open();
  assert.equal((await a.sync.sync(true)).state, 'synced');
  await b.sync.sync(true);
  assert.equal((await b.todos()).filter((t) => t.title === 'Bread').length, 1);

  // Both phones edit revision 2 of milk. B loses and sees a resolvable conflict
  // with both values; its later edit to the same todo waits behind the conflict.
  await a.edit(milk, { action: 'set_title', payload: { title: 'Buy oat milk' } });
  await b.edit(milk, { action: 'set_state', payload: { state: 'open' } });
  await b.edit(milk, { action: 'set_title', payload: { title: 'Buy milk x2' } });
  await a.sync.sync(true);
  s = await b.sync.sync(true);
  assert.deepEqual([s.state, s.pending, s.problems], ['synced', 2, 1]);
  let [bMilk] = (await b.todos()).filter((t) => t.id === milk);
  assert.equal(bMilk.problem?.outcome, 'conflict');
  assert.equal(bMilk.problem?.server?.title, 'Buy oat milk');
  assert.deepEqual([bMilk.title, bMilk.state], ['Buy milk x2', 'open']); // local intent still shown

  // Keep mine: a new operation against the latest revision, then the queued one follows.
  await b.sync.resolve(bMilk.problem!.seq, 'keep_mine');
  s = await b.sync.sync(true);
  assert.deepEqual([s.pending, s.problems], [0, 0]);
  await a.sync.sync(true);
  const view = async (p: ReturnType<typeof phone>) => (await p.todos()).map((t) => [t.id, t.title, t.state]);
  assert.deepEqual(await view(a), await view(b));
  assert.deepEqual((await a.todos()).find((t) => t.id === milk)!.title, 'Buy milk x2');

  // Use server: discarding the conflicted edit adopts the other device's value.
  await a.edit(bread, { action: 'set_title', payload: { title: 'Rye bread' } });
  await b.edit(bread, { action: 'set_title', payload: { title: 'Sourdough' } });
  await a.sync.sync(true);
  await b.sync.sync(true);
  // The server is restored from backup (new epoch) while the conflict is unresolved:
  // the phone bootstraps again and the stuck edit survives the snapshot swap.
  admin('new-epoch');
  s = await b.sync.sync(true);
  assert.deepEqual([s.state, s.pending, s.problems], ['synced', 1, 1]);
  const conflict = (await b.todos()).find((t) => t.id === bread)!.problem!;
  assert.equal(conflict.server?.title, 'Rye bread');
  await b.sync.resolve(conflict.seq, 'discard');
  await b.sync.sync(true);
  assert.equal((await b.todos()).find((t) => t.id === bread)!.title, 'Rye bread');

  // An expired or rejected access token is refreshed transparently.
  await a.tokens.set({ ...(await a.tokens.get())!, access_token: 'expired' });
  await a.edit(bread, { action: 'set_state', payload: { state: 'completed' } });
  assert.equal((await a.sync.sync(true)).state, 'synced');

  // A's cursor is from the old epoch too: 410, bootstrap, and both phones agree.
  await b.sync.sync(true);
  assert.equal((await a.sync.sync(true)).state, 'synced');
  assert.deepEqual(await view(a), await view(b));

  // Backoff: after a failure, unforced syncs wait instead of hammering the server.
  a.net.offline = true;
  await a.sync.sync(true);
  a.net.offline = false;
  const before = a.net.requests;
  await a.sync.sync();
  assert.equal(a.net.requests, before);

  // Revoked device: sync stops asking and keeps the edit for after re-pairing.
  const revokeAs = await b.tokens.get();
  await fetch(`${url}/v1/auth/revoke`, { method: 'POST', headers: { Authorization: `Bearer ${revokeAs!.access_token}` } });
  await b.edit(milk, { action: 'set_title', payload: { title: 'After revoke' } });
  s = await b.sync.sync(true);
  assert.deepEqual([s.state, s.pending], ['auth_required', 1]);
  const requests = b.net.requests;
  assert.equal((await b.sync.sync(true)).state, 'auth_required');
  assert.equal(b.net.requests, requests); // no retry spin
  await b.sync.pair(url, code());
  s = await b.sync.sync(true);
  assert.deepEqual([s.state, s.pending], ['synced', 0]);

  // Sign out refuses while edits are queued unless told to discard, then clears everything.
  b.net.offline = true;
  await b.edit(milk, { action: 'set_title', payload: { title: 'Unsent' } });
  assert.deepEqual(await b.sync.signOut(false), { signedOut: false, pending: 1 });
  assert.deepEqual(await b.sync.signOut(true), { signedOut: true, pending: 0 });
  assert.equal(await b.tokens.get(), null);
  assert.deepEqual([(await b.sync.status()).state, (await b.todos()).length], ['unpaired', 0]);
}
