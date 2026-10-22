import { Stack } from 'expo-router';
import { useApp } from '../../AppState';
import { Speakers } from '../../Speakers';
import { Screen } from '../../ui';

export default function SpeakersScreen() {
  const { sync, version, run } = useApp();
  return (
    <>
      <Stack.Screen options={{ title: 'Speakers' }} />
      <Screen onRefresh={() => run(true)}>
        <Speakers sync={sync} version={version} onEdit={() => run(true)} />
      </Screen>
    </>
  );
}
