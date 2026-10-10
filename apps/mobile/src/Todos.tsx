import { randomUUID } from 'expo-crypto';
import { useSQLiteContext } from 'expo-sqlite';
import { useCallback, useEffect, useState } from 'react';
import { Alert, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { applyLocal, listTodos, type Db, type Todo, type TodoState } from './db';
import { MIN_TOUCH, useTheme } from './theme';

export function Todos() {
  const db = useSQLiteContext() as unknown as Db;
  const t = useTheme();
  const [todos, setTodos] = useState<Todo[]>([]);
  const [draft, setDraft] = useState('');

  const refresh = useCallback(async () => setTodos(await listTodos(db)), [db]);
  useEffect(() => { refresh(); }, [refresh]);

  const run = async (fn: () => Promise<void>) => {
    try {
      await fn();
    } catch (e) {
      Alert.alert('Could not save', String(e)); // edit is rolled back, nothing queued
    }
    await refresh();
  };

  const add = () => {
    const title = draft.trim();
    if (!title) return;
    setDraft('');
    run(() => applyLocal(db, randomUUID(), { action: 'create', payload: { title } }, randomUUID));
  };
  const setState = (todo: Todo, state: TodoState) =>
    run(() => applyLocal(db, todo.id, { action: 'set_state', payload: { state } }, randomUUID));

  return (
    <View style={{ gap: 8 }}>
      <View style={styles.row}>
        <TextInput
          value={draft}
          onChangeText={setDraft}
          onSubmitEditing={add}
          placeholder="New todo"
          placeholderTextColor={t.muted}
          accessibilityLabel="New todo title"
          returnKeyType="done"
          style={[styles.input, { color: t.text, borderColor: t.border, backgroundColor: t.card }]}
        />
        <Pressable onPress={add} accessibilityRole="button" style={styles.button}>
          <Text style={{ color: t.accent, fontWeight: '600' }}>Add</Text>
        </Pressable>
      </View>
      {todos.length === 0 && <Text style={{ color: t.muted }}>No open todos.</Text>}
      {todos.map((todo) => {
        const done = todo.state === 'completed';
        return (
          <View key={todo.id} style={[styles.row, styles.item, { backgroundColor: t.card, borderColor: t.border }]}>
            <Pressable
              onPress={() => setState(todo, done ? 'open' : 'completed')}
              accessibilityRole="checkbox"
              accessibilityState={{ checked: done }}
              accessibilityLabel={todo.title}
              accessibilityHint={done ? 'Reopens the todo' : 'Marks the todo completed'}
              style={[styles.row, { flex: 1, minHeight: MIN_TOUCH }]}
            >
              <Text style={{ color: t.text, fontSize: 20 }}>{done ? '☑' : '☐'}</Text>
              <Text style={{ color: done ? t.muted : t.text, flex: 1, textDecorationLine: done ? 'line-through' : 'none' }}>
                {todo.title}
              </Text>
            </Pressable>
            {todo.pending > 0 && (
              <Text style={{ color: t.badge, fontSize: 12 }} accessibilityLabel={`${todo.pending} changes queued`}>
                Queued
              </Text>
            )}
            <Pressable onPress={() => setState(todo, 'dismissed')} accessibilityRole="button" accessibilityLabel={`Dismiss ${todo.title}`} style={styles.button}>
              <Text style={{ color: t.muted }}>Dismiss</Text>
            </Pressable>
          </View>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  item: { borderWidth: StyleSheet.hairlineWidth, borderRadius: 10, paddingHorizontal: 12 },
  input: { flex: 1, minHeight: MIN_TOUCH, borderWidth: StyleSheet.hairlineWidth, borderRadius: 10, paddingHorizontal: 12, fontSize: 17 },
  button: { minHeight: MIN_TOUCH, minWidth: MIN_TOUCH, justifyContent: 'center', alignItems: 'center', paddingHorizontal: 8 },
});
