import { useState } from 'react';
import { Alert, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import type { Sync, SyncStatus } from './sync';
import { MIN_TOUCH, useTheme } from './theme';

const LABEL: Record<SyncStatus['state'], string> = {
  unpaired: 'Not connected',
  synced: 'Synced',
  offline: 'Offline — changes are saved on this phone',
  auth_required: 'Sign-in expired — pair again to send queued changes',
  error: 'Server error — will retry',
};

export function Connection({ sync, status, onChange }: { sync: Sync; status: SyncStatus | null; onChange: () => void }) {
  const t = useTheme();
  const [url, setUrl] = useState(__DEV__ ? 'http://localhost:8000' : 'https://');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  if (!status) return null;
  const needsPairing = status.state === 'unpaired' || status.state === 'auth_required';

  const pair = async () => {
    setBusy(true);
    setError(undefined);
    try {
      await sync.pair(url, code.trim());
      setCode('');
      await sync.sync(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setBusy(false);
    onChange();
  };

  const signOut = async () => {
    const result = await sync.signOut(false);
    if (result.signedOut) return onChange();
    Alert.alert(
      'Discard unsynced changes?',
      `${result.pending} change${result.pending === 1 ? ' has' : 's have'} not reached the server and will be lost.`,
      [
        { text: 'Cancel', style: 'cancel' },
        { text: 'Discard and sign out', style: 'destructive', onPress: () => sync.signOut(true).then(onChange) },
      ],
    );
  };

  const queued = status.pending ? ` · ${status.pending} queued` : '';
  const problems = status.problems ? ` · ${status.problems} need review` : '';
  const last = status.lastSuccessAt ? `Last sync ${new Date(status.lastSuccessAt).toLocaleString()}` : 'Never synced';

  return (
    <View style={{ gap: 8 }}>
      <Text style={{ color: t.text }} accessibilityLiveRegion="polite">{LABEL[status.state]}{queued}{problems}</Text>
      {status.state !== 'unpaired' && <Text style={{ color: t.muted }}>{last}</Text>}
      {status.error && <Text style={{ color: t.badge }}>{status.error}</Text>}
      {status.restoreLost > 0 && (
        <View style={{ gap: 4 }}>
          <Text style={{ color: t.badge }}>
            The server was restored from a backup. {status.restoreLost} memory edit{status.restoreLost === 1 ? ' was' : 's were'} made
            after that backup and could not be recovered; those memories return to the inbox.
          </Text>
          <Button title="OK" color={t.accent} onPress={() => sync.acknowledgeRestore().then(onChange)} />
        </View>
      )}
      {needsPairing ? (
        <>
          <TextInput
            value={url}
            onChangeText={setUrl}
            editable={status.state === 'unpaired'}
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="url"
            accessibilityLabel="Server address"
            style={[styles.input, { color: t.text, borderColor: t.border, backgroundColor: t.card }]}
          />
          <TextInput
            value={code}
            onChangeText={setCode}
            autoCapitalize="characters"
            autoCorrect={false}
            placeholder="Pairing code"
            placeholderTextColor={t.muted}
            accessibilityLabel="Pairing code"
            onSubmitEditing={pair}
            style={[styles.input, { color: t.text, borderColor: t.border, backgroundColor: t.card }]}
          />
          {error && <Text style={{ color: t.badge }}>{error}</Text>}
          <Button title={busy ? 'Pairing…' : 'Pair'} color={t.accent} disabled={busy || !code.trim()} onPress={pair} />
        </>
      ) : (
        <View style={{ flexDirection: 'row', gap: 16 }}>
          <Button title="Sync now" color={t.accent} onPress={() => sync.sync(true).then(onChange)} />
          <Button title="Sign out" color={t.muted} onPress={signOut} />
        </View>
      )}
    </View>
  );
}

export function Button({ title, color, onPress, disabled }: { title: string; color: string; onPress: () => void; disabled?: boolean }) {
  return (
    <Pressable onPress={onPress} disabled={disabled} accessibilityRole="button" accessibilityState={{ disabled }}
      style={{ minHeight: MIN_TOUCH, justifyContent: 'center', opacity: disabled ? 0.5 : 1 }}>
      <Text style={{ color, fontWeight: '600' }}>{title}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  input: { minHeight: MIN_TOUCH, borderWidth: StyleSheet.hairlineWidth, borderRadius: 10, paddingHorizontal: 12, fontSize: 17 },
});
