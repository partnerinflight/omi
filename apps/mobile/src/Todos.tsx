import { randomUUID } from 'expo-crypto';
import { useSQLiteContext } from 'expo-sqlite';
import { useCallback, useEffect, useState } from 'react';
import { Alert, Pressable, View } from 'react-native';
import Swipeable from 'react-native-gesture-handler/ReanimatedSwipeable';
import { applyLocal, listTodos, type Db, type Problem, type Todo, type TodoState } from './db';
import type { Sync } from './sync';
import { Button, color, Empty, Field, Group, Icon, MIN_TOUCH, Note, Row, styles, T } from './ui';

const ERRORS: Record<string, string> = {
  stale_revision: 'Changed on another device.',
  not_found: 'No longer exists on the server.',
  invalid_title: 'The server rejected this title.',
  invalid_state: 'The server rejected this change.',
  legacy_authority: 'Still managed in Obsidian and Telegram until the migration completes.',
};

export function Todos({ sync, version, onEdit }: { sync: Sync; version: number; onEdit: () => void }) {
  const db = useSQLiteContext() as unknown as Db;
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
    const id = randomUUID();
    setTodos((list) => [{ id, title, state: 'open', pending: 1, problem: null }, ...list]);
    run(() => applyLocal(db, id, { action: 'create', payload: { title } }, randomUUID));
  };
  const setState = (todo: Todo, state: TodoState) => {
    // Show the change now; the queued write and the sync follow in the background.
    setTodos((list) => state === 'dismissed' ? list.filter((t) => t.id !== todo.id)
      : list.map((t) => (t.id === todo.id ? { ...t, state, pending: t.pending + 1 } : t)));
    return run(() => applyLocal(db, todo.id, { action: 'set_state', payload: { state } }, randomUUID));
  };
  const resolve = (problem: Problem, choice: 'keep_mine' | 'discard') => run(() => sync.resolve(problem.seq, choice));

  return (
    <>
      <Group inset={52}>
        <Row>
          <Icon name="plus.circle.fill" size={24} />
          <Field value={draft} onChangeText={setDraft} onSubmitEditing={add} submitBehavior="submit"
            placeholder="New Todo" accessibilityLabel="New todo title" returnKeyType="done" />
        </Row>
        {todos.map((todo) => (
          <TodoRow key={todo.id} todo={todo} onToggle={() => setState(todo, todo.state === 'completed' ? 'open' : 'completed')}
            onDismiss={() => setState(todo, 'dismissed')} onResolve={(choice) => todo.problem && resolve(todo.problem, choice)} />
        ))}
      </Group>
      {todos.length === 0 && <Empty icon="checkmark.circle" title="Nothing open" detail="Todos you add here sync to your other devices." />}
    </>
  );
}

function TodoRow({ todo, onToggle, onDismiss, onResolve }: {
  todo: Todo; onToggle: () => void; onDismiss: () => void; onResolve: (choice: 'keep_mine' | 'discard') => void }) {
  const done = todo.state === 'completed';
  const p = todo.problem;
  return (
    <Swipeable
      friction={2}
      rightThreshold={40}
      overshootRight={false}
      renderRightActions={(_progress, _translation, methods) => (
        <Pressable accessibilityElementsHidden onPress={() => { methods.close(); onDismiss(); }} style={swipe}>
          <Icon name="xmark.bin" tint={color.onTint} size={20} />
          <T ramp="caption1" tone={color.onTint} style={{ fontWeight: '600' }}>Dismiss</T>
        </Pressable>
      )}
    >
      <View style={{ backgroundColor: color.cell }}
        accessible
        accessibilityRole="checkbox"
        accessibilityState={{ checked: done }}
        accessibilityLabel={`${todo.title}${todo.pending > 0 && !p ? ', waiting to sync' : ''}`}
        accessibilityHint={done ? 'Double-tap to reopen' : 'Double-tap to complete'}
        accessibilityActions={[{ name: 'activate' }, { name: 'dismiss', label: 'Dismiss' }]}
        onAccessibilityAction={(e) => (e.nativeEvent.actionName === 'dismiss' ? onDismiss() : onToggle())}
      >
        <View style={[styles.row, { alignItems: 'flex-start' }]}>
          <Pressable onPress={onToggle} hitSlop={10} style={{ minHeight: 22, justifyContent: 'center' }} importantForAccessibility="no">
            <Icon name={done ? 'checkmark.circle.fill' : 'circle'} size={24} tint={done ? color.tint : color.tertiary} />
          </Pressable>
          <View style={{ flex: 1, gap: 6, minHeight: 22, justifyContent: 'center' }}>
            <T tone={done ? color.secondary : color.label} style={done && { textDecorationLine: 'line-through' }}>{todo.title}</T>
            {p && (
              <>
                <Note icon="exclamationmark.triangle.fill" text={ERRORS[p.error] ?? p.error} />
                {p.server && <T ramp="footnote" tone={color.secondary}>On the server: “{p.server.title}”, {p.server.state}</T>}
                <View style={{ flexDirection: 'row', gap: 8, flexWrap: 'wrap' }}>
                  {p.outcome === 'conflict' && p.server && p.error !== 'already_exists' && (
                    <Button kind="tinted" title="Keep Mine" onPress={() => onResolve('keep_mine')} />
                  )}
                  <Button kind="tinted" tone={color.danger} title="Discard Change" onPress={() => onResolve('discard')} />
                </View>
              </>
            )}
          </View>
          {todo.pending > 0 && !p && <Icon name="clock" size={15} tint={color.secondary} style={{ width: 15, height: 15, marginTop: 4 }} />}
        </View>
      </View>
    </Swipeable>
  );
}

const swipe = { width: 88, minHeight: MIN_TOUCH, backgroundColor: color.neutral, alignItems: 'center', justifyContent: 'center', gap: 2 } as const;
