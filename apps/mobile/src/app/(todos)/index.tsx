import { Stack } from 'expo-router';
import { useApp } from '../../AppState';
import { Todos } from '../../Todos';
import { Screen } from '../../ui';

export default function TodosScreen() {
  const { sync, version, run } = useApp();
  return (
    <>
      <Stack.Screen options={{ title: 'Todos' }} />
      <Screen onRefresh={() => run(true)}>
        <Todos sync={sync} version={version} onEdit={() => run()} />
      </Screen>
    </>
  );
}
