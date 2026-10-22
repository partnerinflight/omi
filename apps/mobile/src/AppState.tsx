import { randomUUID } from 'expo-crypto';
import { useSQLiteContext } from 'expo-sqlite';
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { AppState } from 'react-native';
import type { Db } from './db';
import { createSync, type Sync, type SyncStatus } from './sync';
import { keychainTokens } from './tokenStore';

const RETRY_MS = 30_000; // backoff inside sync decides whether a tick actually sends

type App = {
  sync: Sync;
  status: SyncStatus | null;
  version: number; // bumped when local data may have changed
  run: (force?: boolean) => Promise<void>;
  changed: () => Promise<void>;
};

const Ctx = createContext<App | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const db = useSQLiteContext() as unknown as Db;
  const sync = useMemo(() => createSync(db, keychainTokens, { newId: randomUUID, allowHttpLocalhost: __DEV__ }), [db]);
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [version, setVersion] = useState(0);

  const changed = useCallback(async () => {
    setStatus(await sync.status());
    setVersion((v) => v + 1);
  }, [sync]);
  const run = useCallback((force = false) => sync.sync(force).then(changed), [sync, changed]);

  // Sync on open, on resume and on a timer; edits trigger it too.
  useEffect(() => {
    run();
    const sub = AppState.addEventListener('change', (s) => s === 'active' && run(true)); // back in the app: try now
    const timer = setInterval(() => AppState.currentState === 'active' && run(), RETRY_MS);
    return () => {
      sub.remove();
      clearInterval(timer);
    };
  }, [run]);

  const value = useMemo(() => ({ sync, status, version, run, changed }), [sync, status, version, run, changed]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp() {
  const app = useContext(Ctx);
  if (!app) throw new Error('useApp outside AppProvider');
  return app;
}
