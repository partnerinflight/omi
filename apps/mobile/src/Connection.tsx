import { useState } from 'react';
import { Alert, View } from 'react-native';
import type { Sync, SyncStatus } from './sync';
import { ActionRow, color, Field, Group, Icon, Row, T } from './ui';

const STATE: Record<SyncStatus['state'], { label: string; icon: 'checkmark.circle.fill' | 'wifi.slash' | 'link' | 'exclamationmark.triangle.fill' | 'key.slash.fill'; tone: typeof color.tint }> = {
  unpaired: { label: 'Not connected', icon: 'link', tone: color.secondary },
  synced: { label: 'Synced', icon: 'checkmark.circle.fill', tone: color.success },
  offline: { label: 'Offline — changes are saved on this phone', icon: 'wifi.slash', tone: color.secondary },
  auth_required: { label: 'Sign-in expired — pair again to send queued changes', icon: 'key.slash.fill', tone: color.warning },
  error: { label: 'Server error — will retry', icon: 'exclamationmark.triangle.fill', tone: color.warning },
};

export function Connection({ sync, status, onChange }: { sync: Sync; status: SyncStatus | null; onChange: () => void }) {
  const [url, setUrl] = useState(__DEV__ ? 'http://localhost:8000' : 'https://');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  if (!status) return null;
  const needsPairing = status.state === 'unpaired' || status.state === 'auth_required';
  const s = STATE[status.state];

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
        { text: 'Discard and Sign Out', style: 'destructive', onPress: () => sync.signOut(true).then(onChange) },
      ],
    );
  };

  const counts = [status.pending ? `${status.pending} queued` : '', status.problems ? `${status.problems} need review` : '']
    .filter(Boolean).join(' · ');
  const last = status.lastSuccessAt ? `Last sync ${new Date(status.lastSuccessAt).toLocaleString()}` : 'Never synced';

  return (
    <>
      {status.restoreLost > 0 && (
        <Group header="Server restored">
          <View style={{ padding: 16, gap: 6 }}>
            <T>
              The server was restored from a backup. {status.restoreLost} memory edit{status.restoreLost === 1 ? ' was' : 's were'} made
              after that backup and could not be recovered; those memories return to the inbox.
            </T>
          </View>
          <ActionRow title="OK" onPress={() => sync.acknowledgeRestore().then(onChange)} />
        </Group>
      )}

      <Group header="Sync" footer={needsPairing ? 'Create a code on the server with: python -m sb_server pair "My iPhone"' : undefined}>
        <Row>
          <Icon name={s.icon} tint={s.tone} size={22} />
          <View style={{ flex: 1, gap: 2 }} accessibilityLiveRegion="polite">
            <T>{s.label}</T>
            {(status.state !== 'unpaired' || counts) && (
              <T ramp="footnote" tone={color.secondary}>{[status.state !== 'unpaired' ? last : '', counts].filter(Boolean).join(' · ')}</T>
            )}
            {!!status.error && <T ramp="footnote" tone={color.warning}>{status.error}</T>}
          </View>
        </Row>
        {needsPairing && (
          <Row>
            <T style={{ width: 72 }}>Server</T>
            <Field value={url} onChangeText={setUrl} editable={status.state === 'unpaired'} autoCapitalize="none" autoCorrect={false}
              keyboardType="url" accessibilityLabel="Server address" style={status.state !== 'unpaired' && { color: color.secondary }} />
          </Row>
        )}
        {needsPairing && (
          <Row>
            <T style={{ width: 72 }}>Code</T>
            <Field value={code} onChangeText={setCode} autoCapitalize="characters" autoCorrect={false} placeholder="Pairing code"
              accessibilityLabel="Pairing code" onSubmitEditing={pair} returnKeyType="go" />
          </Row>
        )}
        {needsPairing && !!error && (
          <View style={{ paddingHorizontal: 16, paddingVertical: 11 }}><T ramp="footnote" tone={color.danger}>{error}</T></View>
        )}
        {needsPairing
          ? <ActionRow title={busy ? 'Pairing…' : 'Pair This iPhone'} icon="link" disabled={busy || !code.trim()} onPress={pair} />
          : <ActionRow title="Sync Now" icon="arrow.triangle.2.circlepath" onPress={() => sync.sync(true).then(onChange)} />}
      </Group>

      {!needsPairing && (
        <Group>
          <ActionRow title="Sign Out" tone={color.danger} onPress={signOut} />
        </Group>
      )}
    </>
  );
}
