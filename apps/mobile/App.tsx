import { SQLiteProvider } from 'expo-sqlite';
import { StatusBar } from 'expo-status-bar';
import type { ReactNode } from 'react';
import { ScrollView, Text, View } from 'react-native';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';
import { ClipCheck } from './src/ClipCheck';
import { CredentialCheck } from './src/CredentialCheck';
import { migrate, type Db } from './src/db';
import { Todos } from './src/Todos';
import { useTheme } from './src/theme';

// Milestone 0 spike: one screen per native boundary from docs/mobile/implementation-plan.md.
export default function App() {
  const t = useTheme();
  return (
    <SafeAreaProvider>
      <SQLiteProvider databaseName="secondbrain.db" onInit={(db) => migrate(db as unknown as Db)}>
        <SafeAreaView style={{ flex: 1, backgroundColor: t.bg }}>
          <ScrollView contentContainerStyle={{ padding: 16, gap: 24 }} keyboardShouldPersistTaps="handled">
            <Section title="Todos (offline, SQLite + outbox)"><Todos /></Section>
            <Section title="Review clip playback"><ClipCheck /></Section>
            <Section title="Keychain credential"><CredentialCheck /></Section>
          </ScrollView>
          <StatusBar style="auto" />
        </SafeAreaView>
      </SQLiteProvider>
    </SafeAreaProvider>
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
