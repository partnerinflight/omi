import { Directory, File, Paths } from 'expo-file-system';
import type { Sync } from './sync';

// Review clips are downloaded into the cache directory and played from there: the server sends
// whole files without range support, which the iOS player needs to stream, and a cached clip
// plays offline. iOS may purge the cache; a missing clip is fetched again on demand.
// ponytail: keyed by observation and clip number only; a clip the worker re-uploads keeps the old copy.
// A clip is deleted once its speaker is named or discarded, here or on Windows.
const dir = new Directory(Paths.cache, 'clips');
const inflight = new Map<string, Promise<string>>();

const file = (observation: string, n: number) => new File(dir, `${observation}-${n}.wav`);

export function cachedClip(observation: string, n: number): string | null {
  const f = file(observation, n);
  return f.exists ? f.uri : null;
}

export function fetchClip(sync: Sync, observation: string, n: number, auth?: Promise<Auth>): Promise<string> {
  const hit = cachedClip(observation, n);
  if (hit) return Promise.resolve(hit);
  const key = `${observation}/${n}`;
  let job = inflight.get(key);
  if (!job) {
    job = (async () => {
      const { base, headers } = await (auth ?? sync.clipAuth());
      if (!dir.exists) dir.create({ intermediates: true, idempotent: true });
      const f = await File.downloadFileAsync(`${base}/v1/clips/${encodeURIComponent(observation)}/${n}`,
        file(observation, n), { headers, idempotent: true });
      return f.uri;
    })().finally(() => inflight.delete(key));
    inflight.set(key, job);
  }
  return job;
}

const owner = (f: File | Directory) => f.name.replace(/-\d+\.wav$/, '');

// Named or discarded on this phone: its clips are not needed again.
export function deleteClips(observation: string) {
  if (!dir.exists) return;
  for (const f of dir.list()) if (f instanceof File && owner(f) === observation) f.delete();
}

// After a fresh list from the server: drop clips of speakers no longer waiting for review
// (named or discarded on Windows or another device).
export function pruneClips(keep: Set<string>) {
  if (!dir.exists) return;
  for (const f of dir.list()) if (!keep.has(owner(f))) f.delete();
}

// Sign-out: every downloaded clip.
export function clearClips() {
  if (dir.exists) dir.delete();
}

type Auth = Awaited<ReturnType<Sync['clipAuth']>>;

// One at a time in the background, sharing one token check; stops when `cancelled` turns true.
export async function prefetchClips(sync: Sync, clips: [string, number][], cancelled: () => boolean) {
  const missing = clips.filter(([o, n]) => !cachedClip(o, n));
  if (missing.length === 0) return;
  const auth = sync.clipAuth();
  auth.catch(() => {}); // handled per download below
  for (const [o, n] of missing) {
    if (cancelled()) return;
    try {
      await fetchClip(sync, o, n, auth);
    } catch {
      return; // offline or token trouble; a tap retries with a fresh token
    }
  }
}
