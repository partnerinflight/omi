import { randomUUID } from 'expo-crypto';
import { useSQLiteContext } from 'expo-sqlite';
import { useCallback, useEffect, useState } from 'react';
import { Alert, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { Button } from './Connection';
import { applyLocal, listTodos, type Db, type Problem, type Todo, type TodoState } from './db';
import type { Sync } from './sync';
import { MIN_TOUCH, useTheme } from './theme';

const ERRORS: Record<string, string> = {
  stale_revision: 'Changed on another device.',
  not_found: 'No longer exists on the server.',
  invalid_title: 'The server rejected this title.',
  invalid_state: 'The server rejected this change.',
  legacy_authority: 'Still managed in Obsidian and Telegram until the migration completes.',
};

export function Todos({ sync, version, onEdit }: { sync: Sync; version: number; onEdit: () => void }) {
  const db = useSQLiteContext() as unknown as Db;
  const t = useTheme();
  const [todos, setTodos] = useState<Todo[]>([]);
  const [draft, setDraft] = useState('');

  const refresh = useCallback(async () => setTodos(await listTodos(db)), [db]);
  useEffect(() => { refresh(); }, [refresh, version]);

  const run = async (fn: () => Promise<void>) => {
    try {
      await fn();
    } catch (e) {
      Alert.alert('Could not save', String(e)); // edit is rolled back, nothing queued
    }
    await refresh();
    onEdit();
  };

  const add = () => {
    const title = draft.trim();
    if (!title) return;
    setDraft('');
    run(() => applyLocal(db, randomUUID(), { action: 'create', payload: { title } }, randomUUID));
  };
  const setState = (todo: Todo, state: TodoState) =>
    run(() => applyLocal(db, todo.id, { action: 'set_state', payload: { state } }, randomUUID));
  const resolve = (problem: Problem, choice: 'keep_mine' | 'discard') => run(() => sync.resolve(problem.seq, choice));

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
        const p = todo.problem;
        return (
          <View key={todo.id} style={[styles.item, { backgroundColor: t.card, borderColor: p ? t.badge : t.border }]}>
            <View style={styles.row}>
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
              {todo.pending > 0 && !p && (
                <Text style={{ color: t.badge, fontSize: 12 }} accessibilityLabel={`${todo.pending} changes waiting to sync`}>
                  Queued
                </Text>
              )}
              <Pressable onPress={() => setState(todo, 'dismissed')} accessibilityRole="button" accessibilityLabel={`Dismiss ${todo.title}`} style={styles.button}>
                <Text style={{ color: t.muted }}>Dismiss</Text>
              </Pressable>
            </View>
            {p && (
              <View style={{ paddingBottom: 8, gap: 4 }}>
                <Text style={{ color: t.badge }}>{ERRORS[p.error] ?? p.error}</Text>
                {p.server && (
                  <Text style={{ color: t.muted }}>Server: “{p.server.title}”, {p.server.state}</Text>
                )}
                <View style={{ flexDirection: 'row', gap: 16 }}>
                  {p.outcome === 'conflict' && p.server && p.error !== 'already_exists' && (
                    <Button title="Keep mine" color={t.accent} onPress={() => resolve(p, 'keep_mine')} />
                  )}
                  <Button title="Discard this change" color={t.muted} onPress={() => resolve(p, 'discard')} />
                </View>
              </View>
            )}
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
