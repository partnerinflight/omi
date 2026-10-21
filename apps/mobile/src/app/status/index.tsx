import { Stack } from 'expo-router';
import { useApp } from '../../AppState';
import { ClipCheck } from '../../ClipCheck';
import { Connection } from '../../Connection';
import { Pipeline } from '../../Pipeline';
import { Screen } from '../../ui';

export default function StatusScreen() {
  const { sync, status, version, run, changed } = useApp();
  return (
    <>
      <Stack.Screen options={{ title: 'Status' }} />
      <Screen onRefresh={() => run(true)}>
        <Connection sync={sync} status={status} onChange={changed} />
        <Pipeline sync={sync} version={version} />
        <ClipCheck />
      </Screen>
    </>
  );
}
