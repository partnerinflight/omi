import { useAudioPlayer, useAudioPlayerStatus } from 'expo-audio';
import { Directory, File, Paths } from 'expo-file-system';
import { useRef, useState } from 'react';
import { Alert } from 'react-native';
import type { Sync } from './sync';

// Clips are downloaded into the cache directory and played from there: the server sends whole
// files without range support, which the iOS player needs to stream, and a cached clip plays
// offline. iOS may purge the cache; a missing clip is fetched again on demand.
// ponytail: keyed by name only; a clip the worker re-uploads keeps the old copy.

export type Clip = { key: string; path: string }; // cache file name, server path
type Auth = Awaited<ReturnType<Sync['clipAuth']>>;

function clipStore(folder: string) {
  const dir = new Directory(Paths.cache, folder);
  const inflight = new Map<string, Promise<string>>();
  const file = (key: string) => new File(dir, `${key}.wav`);
  const cached = (key: string) => {
    const f = file(key);
    return f.exists ? f.uri : null;
  };

  function fetch(sync: Sync, clip: Clip, auth?: Promise<Auth>): Promise<string> {
    const hit = cached(clip.key);
    if (hit) return Promise.resolve(hit);
    let job = inflight.get(clip.key);
    if (!job) {
      job = (async () => {
        const { base, headers } = await (auth ?? sync.clipAuth());
        if (!dir.exists) dir.create({ intermediates: true, idempotent: true });
        return (await File.downloadFileAsync(base + clip.path, file(clip.key), { headers, idempotent: true })).uri;
      })().finally(() => inflight.delete(clip.key));
      inflight.set(clip.key, job);
    }
    return job;
  }

  return {
    fetch,
    // One at a time in the background, sharing one token check; stops when `cancelled` turns true.
    async prefetch(sync: Sync, clips: Clip[], cancelled: () => boolean) {
      const missing = clips.filter((c) => !cached(c.key));
      if (missing.length === 0) return;
      const auth = sync.clipAuth();
      auth.catch(() => {}); // handled per download below
      for (const clip of missing) {
        if (cancelled()) return;
        try {
          await fetch(sync, clip, auth);
        } catch {
          return; // offline or token trouble; a tap retries with a fresh token
        }
      }
    },
    // Done with on this phone (a speaker named, a todo completed).
    remove(keys: string[]) {
      for (const key of keys) {
        const f = file(key);
        if (f.exists) f.delete();
      }
    },
    // After a fresh list from the server: drop what is no longer listed (done elsewhere).
    prune(keep: Set<string>) {
      if (!dir.exists) return;
      for (const f of dir.list()) if (!keep.has(f.name.replace(/\.wav$/, ''))) f.delete();
    },
    clear() {
      if (dir.exists) dir.delete();
    },
  };
}
export type ClipStore = ReturnType<typeof clipStore>;

// Speaker review clips, until the speaker is named or discarded; todo clips, while the todo is open.
export const speakerClips = clipStore('clips');
export const todoClips = clipStore('todo-clips');

// Sign-out: every downloaded clip.
export function clearClips() {
  speakerClips.clear();
  todoClips.clear();
}

// One player per page: a native player per clip button made long lists unusable.
export function useClipPlayer(sync: Sync, store: ClipStore) {
  const player = useAudioPlayer(null);
  const status = useAudioPlayerStatus(player);
  const [current, setCurrent] = useState<string | null>(null);
  const [loading, setLoading] = useState<string | null>(null);
  const taps = useRef(0);
  const toggle = async (clip: Clip) => {
    const tap = ++taps.current;
    if (clip.key === current && status.playing) return player.pause();
    if (clip.key !== current) {
      setLoading(clip.key);
      let uri: string;
      try {
        uri = await store.fetch(sync, clip);
      } catch {
        if (tap === taps.current) setLoading(null);
        return Alert.alert('Clip unavailable', 'Connect to the server to play clips.');
      }
      if (tap !== taps.current) return; // another clip was tapped while this one downloaded
      setLoading(null);
      player.replace({ uri });
      setCurrent(clip.key);
    } else if (status.didJustFinish || status.currentTime >= status.duration) {
      player.seekTo(0);
    }
    player.play();
  };
  return { playing: status.playing ? current : null, loading, toggle };
}
export type ClipPlayer = ReturnType<typeof useClipPlayer>;
