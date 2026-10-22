import { Stack, useLocalSearchParams } from 'expo-router';
import { useApp } from '../../AppState';
import { ClipCheck } from '../../ClipCheck';
import { Connection } from '../../Connection';
import { Pipeline } from '../../Pipeline';
import { Screen } from '../../ui';

export default function StatusScreen() {
  const { sync, status, version, run, changed } = useApp();
  // Set by +native-intent from the server's pairing QR code.
  const { server, code } = useLocalSearchParams<{ server?: string; code?: string }>();
  return (
    <>
      <Stack.Screen options={{ title: 'Status' }} />
      <Screen onRefresh={() => run(true)}>
        <Connection key={`${server}|${code}`} sync={sync} status={status} onChange={changed}
          link={server && code ? { server, code } : undefined} />
        <Pipeline sync={sync} version={version} />
        <ClipCheck />
      </Screen>
    </>
  );
}
