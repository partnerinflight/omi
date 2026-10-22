// Backup/restore drill at the deployment boundary: `npm run test:restore` (Docker).
// The server image runs with deploy/selfhost/secondbrain.yml's hardening, against postgres:14
// with the README's secondbrain role. Backup is HA_Selfhost's `pg_dumpall`; restore goes into
// a fresh Postgres, then `new-epoch`. Two phones must lose nothing, including edits the server
// acknowledged after the backup.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { applyLocal, applyMemory, listMemories, listTodos, migrate, type Db } from '../src/db.ts';
import { createSync, type Tokens, type TokenStore } from '../src/sync.ts';
import { openDb } from './sqlite.ts';

const serverDir = new URL('../../../server/', import.meta.url).pathname;
const tag = randomUUID().slice(0, 8);
const net = `sb-drill-${tag}`;
const names = { pgA: `sb-drill-pga-${tag}`, pgB: `sb-drill-pgb-${tag}`, api: `sb-drill-api-${tag}` };
const docker = (...args: string[]) => execFileSync('docker', args, { encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] }).trim();
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const port = await new Promise<number>((resolve) => {
  const s = createServer().listen(0, '127.0.0.1', () => {
    const { port } = s.address() as { port: number };
    s.close(() => resolve(port));
  });
});
const url = `http://127.0.0.1:${port}`;

async function healthy() {
  for (let i = 0; i < 150; i++) {
    try {
      if ((await fetch(`${url}/v1/health`)).ok) return;
    } catch {}
    await sleep(200);
  }
  throw new Error('server did not become healthy');
}

function postgres(name: string, ...alias: string[]) {
  docker('run', '-d', '--name', name, '--network', net, ...alias, '-e', 'POSTGRES_USER=postgres', '-e', 'POSTGRES_PASSWORD=admin', 'postgres:14-alpine');
  for (let i = 0; ; i++) {
    try {
      // Over TCP: the image's first-boot init server listens on the socket only and then restarts,
      // so a socket check could pass just before the restart and the next command fail.
      docker('exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres');
      docker('exec', name, 'psql', '-h', '127.0.0.1', '-U', 'postgres', '-c', 'SELECT 1');
      return;
    } catch (e) {
      if (i > 120) throw e;
      execFileSync('sleep', ['0.5']);
    }
  }
}

const sb = (...args: string[]) => docker('exec', names.api, 'python', '-m', 'sb_server', ...args);

try {
  docker('network', 'create', net);
  // The API reaches the database by the alias, so the restore can move it to a fresh server.
  postgres(names.pgA, '--network-alias', 'postgres-db');
  // README step 3: an owner role and database separate from the superuser.
  docker('exec', names.pgA, 'psql', '-U', 'postgres', '-c', "CREATE ROLE secondbrain LOGIN PASSWORD 'drill'");
  docker('exec', names.pgA, 'psql', '-U', 'postgres', '-c', 'CREATE DATABASE secondbrain OWNER secondbrain');
  const image = docker('build', '-q', serverDir);
  docker('run', '-d', '--name', names.api, '--network', net, '-p', `127.0.0.1:${port}:8000`,
    '--user', '65532:65532', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
    '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m,mode=1777', '--pids-limit', '256', '--restart', 'unless-stopped',
    '-e', 'SB_DATABASE_URL=postgresql://secondbrain:drill@postgres-db:5432/secondbrain', '-e', 'SB_FORWARDED_ALLOW_IPS=*', image);
  await healthy();

  // Hardening holds at runtime: unprivileged, read-only root, no capabilities.
  assert.equal(docker('exec', names.api, 'id', '-u'), '65532');
  assert.throws(() => docker('exec', names.api, 'touch', '/app/x'));
  assert.equal(docker('inspect', '-f', '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}}', names.api), 'true [ALL]');

  // A crash (the process exits) is restarted by the policy, and migrations are idempotent.
  docker('exec', names.api, 'python', '-c', 'import os, signal; os.kill(1, signal.SIGTERM)');
  for (let i = 0; i < 100 && docker('inspect', '-f', '{{.RestartCount}}', names.api) === '0'; i++) await sleep(200);
  assert.equal(docker('inspect', '-f', '{{.RestartCount}}', names.api), '1');
  await healthy();

  await drill();
  console.log('restore.test.ts: ok');
} finally {
  for (const n of Object.values(names)) {
    try { docker('rm', '-f', n); } catch {}
  }
  try { docker('network', 'rm', net); } catch {}
}

function phone(name: string) {
  const path = `${process.env.TMPDIR ?? '/tmp'}/sb-drill-${name}-${randomUUID()}.db`;
  let saved: Tokens | null = null;
  const tokens: TokenStore = { get: async () => saved, set: async (t) => void (saved = t), clear: async () => void (saved = null) };
  const net = { offline: false, dropAfterBootstrap: false };
  const db: Db = openDb(path);
  const sync = createSync(db, tokens, {
    fetch: async (i, init) => {
      if (net.offline) throw new TypeError('Network request failed');
      const res = await fetch(i, init);
      if (net.dropAfterBootstrap && String(i).includes('/v1/bootstrap')) net.offline = true; // connection lost mid-sync
      return res;
    },
    newId: randomUUID, allowHttpLocalhost: true,
  });
  return { db, sync, net, edit: (id: string, op: Parameters<typeof applyLocal>[2]) => applyLocal(db, id, op, randomUUID) };
}

async function drill() {
  const a = phone('a');
  const b = phone('b');
  await migrate(a.db);
  await migrate(b.db);
  await a.sync.pair(url, sb('pair', 'a').split(/\s+/)[0]);
  await b.sync.pair(url, sb('pair', 'b').split(/\s+/)[0]);
  const worker = sb('add-producer', 'drill-pc');
  const items = async (...ids: string[]) => {
    const res = await fetch(`${url}/v1/worker/events`, { method: 'POST', headers: { Authorization: `Bearer ${worker}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ records: ids.map((id) => ({ kind: 'item', id, hash: id, data: { kind: 'fact', text: `Fact ${id}` } })) }) });
    assert.equal(res.status, 200);
  };

  const milk = randomUUID();
  await a.edit(milk, { action: 'create', payload: { title: 'Buy milk' } });
  await items('m-old');
  await a.sync.sync(true);
  await b.sync.sync(true);

  // --- Backup, exactly as HA_Selfhost's backup_databases.py does it.
  const dump = `${process.env.TMPDIR ?? '/tmp'}/sb-drill-${tag}.sql`;
  writeFileSync(dump, docker('exec', names.pgA, 'sh', '-c', 'exec pg_dumpall --username="$POSTGRES_USER"'));

  // After the backup the server accepts more, which the restore will not have.
  const bread = randomUUID();
  await a.edit(bread, { action: 'create', payload: { title: 'Bread (after backup)' } });
  await a.edit(milk, { action: 'set_state', payload: { state: 'completed' } });
  await applyMemory(a.db, 'm-old', { action: 'set_text', payload: { text: 'Fact m-old, corrected after backup' } }, randomUUID);
  await items('m-new');
  await a.sync.sync(true);
  await applyMemory(a.db, 'm-new', { action: 'set_state', payload: { state: 'confirmed' } }, randomUUID);
  assert.equal((await a.sync.sync(true)).pending, 0); // all acknowledged
  await b.sync.sync(true);
  b.net.offline = true;
  const eggs = randomUUID();
  await b.edit(eggs, { action: 'create', payload: { title: 'Eggs (offline during restore)' } });

  // --- Restore into a fresh Postgres, move the alias, reset the epoch.
  postgres(names.pgB);
  execFileSync('docker', ['exec', '-i', names.pgB, 'psql', '-q', '-U', 'postgres', '-d', 'postgres'], { input: execFileSync('cat', [dump]), stdio: ['pipe', 'ignore', 'ignore'] });
  docker('rm', '-f', names.pgA);
  docker('network', 'disconnect', net, names.pgB);
  docker('network', 'connect', '--alias', 'postgres-db', net, names.pgB); // the API reconnects per request
  await healthy();
  assert.match(sb('new-epoch'), /^epoch: /);
  // new-epoch revoked every credential: both phones see sign-in expired, keep their queued
  // edits, and pair again with the same server (the drill uses no worker token after this).
  assert.equal((await a.sync.sync(true)).state, 'auth_required');
  await a.sync.pair(url, sb('pair', 'a').split(/\s+/)[0]);

  // Both phones held the lost acknowledgments and queue them again. B loses its connection
  // right after bootstrapping, so A's copies land first and B's identical ones must not conflict.
  b.net.offline = false;
  await b.sync.pair(url, sb('pair', 'b').split(/\s+/)[0]); // b was offline through the restore
  b.net.dropAfterBootstrap = true;
  assert.equal((await b.sync.sync(true)).state, 'offline');
  let s = await a.sync.sync(true);
  assert.deepEqual([s.state, s.pending, s.problems, s.restoreLost], ['synced', 0, 0, 1]); // m-new is gone from the backup
  b.net.offline = false;
  b.net.dropAfterBootstrap = false;
  s = await b.sync.sync(true);
  assert.deepEqual([s.state, s.pending, s.problems], ['synced', 0, 0]); // also replays its offline edit
  await a.sync.sync(true);

  const view = async (p: ReturnType<typeof phone>) => (await listTodos(p.db)).map((t) => [t.title, t.state]).sort();
  const expected = [['Bread (after backup)', 'open'], ['Buy milk', 'completed'], ['Eggs (offline during restore)', 'open']];
  assert.deepEqual(await view(a), expected);
  assert.deepEqual(await view(b), expected);
  const [old] = await listMemories(a.db, { query: 'm-old' });
  assert.equal(old.text, 'Fact m-old, corrected after backup');
  assert.equal((await listMemories(a.db, { query: 'm-new' })).length, 0); // until the worker resends it (bridge test)
  await a.sync.acknowledgeRestore();
  assert.equal((await a.sync.status()).restoreLost, 0);
}
