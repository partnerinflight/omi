import { SQLiteProvider } from 'expo-sqlite';
import { NativeTabs } from 'expo-router/unstable-native-tabs';
import { GestureHandlerRootView } from 'react-native-gesture-handler';
import { AppProvider, useApp } from '../AppState';
import { migrate, type Db } from '../db';
import { color } from '../ui';

export default function RootLayout() {
  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <SQLiteProvider databaseName="secondbrain.db" onInit={(db) => migrate(db as unknown as Db)}>
        <AppProvider>
          <Tabs />
        </AppProvider>
      </SQLiteProvider>
    </GestureHandlerRootView>
  );
}

function Tabs() {
  const { status } = useApp();
  // The Status tab carries a badge when something there needs the user.
  const attention = !!status && (status.state === 'unpaired' || status.state === 'auth_required' || status.problems > 0 || status.restoreLost > 0);
  return (
    <NativeTabs tintColor={color.tint}>
      <NativeTabs.Trigger name="(todos)">
        <NativeTabs.Trigger.Icon sf={{ default: 'checklist', selected: 'checklist' }} />
        <NativeTabs.Trigger.Label>Todos</NativeTabs.Trigger.Label>
      </NativeTabs.Trigger>
      <NativeTabs.Trigger name="memories">
        <NativeTabs.Trigger.Icon sf={{ default: 'brain', selected: 'brain.fill' }} />
        <NativeTabs.Trigger.Label>Memories</NativeTabs.Trigger.Label>
      </NativeTabs.Trigger>
      <NativeTabs.Trigger name="speakers">
        <NativeTabs.Trigger.Icon sf={{ default: 'person.wave.2', selected: 'person.wave.2.fill' }} />
        <NativeTabs.Trigger.Label>Speakers</NativeTabs.Trigger.Label>
      </NativeTabs.Trigger>
      <NativeTabs.Trigger name="status">
        <NativeTabs.Trigger.Icon sf={{ default: 'antenna.radiowaves.left.and.right', selected: 'antenna.radiowaves.left.and.right' }} />
        <NativeTabs.Trigger.Label>Status</NativeTabs.Trigger.Label>
        {attention && <NativeTabs.Trigger.Badge />}
      </NativeTabs.Trigger>
    </NativeTabs>
  );
}
