import { Stack } from 'expo-router';
import { useState } from 'react';
import { useApp } from '../../AppState';
import { Memories } from '../../Memories';
import { Screen } from '../../ui';

export default function MemoriesScreen() {
  const { sync, version, run } = useApp();
  const [query, setQuery] = useState('');
  return (
    <>
      <Stack.Screen options={{
        title: 'Memories',
        headerSearchBarOptions: {
          placeholder: 'Text, person, project or date',
          autoCapitalize: 'none',
          hideWhenScrolling: false,
          onChangeText: (e) => setQuery(e.nativeEvent.text),
          onCancelButtonPress: () => setQuery(''),
        },
      }} />
      <Screen onRefresh={() => run(true)}>
        <Memories sync={sync} version={version} query={query} onEdit={() => run()} />
      </Screen>
    </>
  );
}
