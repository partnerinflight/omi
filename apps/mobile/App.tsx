import { randomUUID } from 'expo-crypto';
import { SQLiteProvider, useSQLiteContext } from 'expo-sqlite';
import { StatusBar } from 'expo-status-bar';
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { AppState, RefreshControl, ScrollView, Text, View } from 'react-native';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';
import { ClipCheck } from './src/ClipCheck';
import { Connection } from './src/Connection';
import { migrate, type Db } from './src/db';
import { createSync, type SyncStatus } from './src/sync';
import { useTheme } from './src/theme';
import { keychainTokens } from './src/tokenStore';
import { Todos } from './src/Todos';

const RETRY_MS = 30_000; // backoff inside sync decides whether a tick actually sends

export default function App() {
  return (
    <SafeAreaProvider>
      <SQLiteProvider databaseName="secondbrain.db" onInit={(db) => migrate(db as unknown as Db)}>
        <Main />
      </SQLiteProvider>
    </SafeAreaProvider>
  );
}

function Main() {
  const t = useTheme();
  const db = useSQLiteContext() as unknown as Db;
  const sync = useMemo(() => createSync(db, keychainTokens, { newId: randomUUID, allowHttpLocalhost: __DEV__ }), [db]);
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [version, setVersion] = useState(0); // bumped when local data may have changed
  const [refreshing, setRefreshing] = useState(false);

  const changed = useCallback(async () => {
    setStatus(await sync.status());
    setVersion((v) => v + 1);
  }, [sync]);
  const run = useCallback((force = false) => sync.sync(force).then(changed), [sync, changed]);

  // Sync on open, on resume and on a timer; edits trigger it too (below).
  useEffect(() => {
    run();
    const sub = AppState.addEventListener('change', (s) => s === 'active' && run());
    const timer = setInterval(() => AppState.currentState === 'active' && run(), RETRY_MS);
    return () => {
      sub.remove();
      clearInterval(timer);
    };
  }, [run]);

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: t.bg }}>
      <ScrollView
        contentContainerStyle={{ padding: 16, gap: 24 }}
        keyboardShouldPersistTaps="handled"
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => {
          setRefreshing(true);
          run(true).finally(() => setRefreshing(false));
        }} />}
      >
        <Section title="Todos"><Todos sync={sync} version={version} onEdit={() => run()} /></Section>
        <Section title="Connection"><Connection sync={sync} status={status} onChange={changed} /></Section>
        <Section title="Review clip playback"><ClipCheck /></Section>
      </ScrollView>
      <StatusBar style="auto" />
    </SafeAreaView>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  const t = useTheme();
  return (
    <View style={{ gap: 8 }}>
      <Text accessibilityRole="header" style={{ color: t.text, fontSize: 22, fontWeight: '700' }}>{title}</Text>
      {children}
    </View>
  );
}
