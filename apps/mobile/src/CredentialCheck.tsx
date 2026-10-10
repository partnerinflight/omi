import * as SecureStore from 'expo-secure-store';
import { randomUUID } from 'expo-crypto';
import { useEffect, useState } from 'react';
import { Pressable, Text, View } from 'react-native';
import { MIN_TOUCH, useTheme } from './theme';

const KEY = 'refresh_token';
// Not restored to another device from backup; matches device-scoped credentials.
const OPTS = { keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY };

export function CredentialCheck() {
  const t = useTheme();
  const [stored, setStored] = useState<string | null | undefined>(undefined);
  const [error, setError] = useState<string>();

  const load = () => SecureStore.getItemAsync(KEY, OPTS).then(setStored, (e) => setError(String(e)));
  useEffect(() => { load(); }, []);

  const act = (fn: () => Promise<void>) => fn().then(load, (e) => setError(String(e)));

  const label = stored === undefined ? 'Reading Keychain…' : stored ? `Stored token …${stored.slice(-6)}` : 'No token stored';
  return (
    <View style={{ gap: 8 }}>
      <Text style={{ color: t.text }}>{label}</Text>
      {error && <Text style={{ color: t.badge }}>{error}</Text>}
      <View style={{ flexDirection: 'row', gap: 12 }}>
        <Btn title="Save synthetic token" color={t.accent} onPress={() => act(() => SecureStore.setItemAsync(KEY, `synthetic-${randomUUID()}`, OPTS))} />
        <Btn title="Delete" color={t.muted} onPress={() => act(() => SecureStore.deleteItemAsync(KEY, OPTS))} />
      </View>
    </View>
  );
}

function Btn({ title, color, onPress }: { title: string; color: string; onPress: () => void }) {
  return (
    <Pressable onPress={onPress} accessibilityRole="button" style={{ minHeight: MIN_TOUCH, justifyContent: 'center' }}>
      <Text style={{ color, fontWeight: '600' }}>{title}</Text>
    </Pressable>
  );
}
