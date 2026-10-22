import { randomUUID } from 'expo-crypto';
import { useSQLiteContext } from 'expo-sqlite';
import { useCallback, useEffect, useState } from 'react';
import { Alert, Pressable, View } from 'react-native';
import Swipeable from 'react-native-gesture-handler/ReanimatedSwipeable';
import { applyMemory, listMemories, type Db, type Memory, type MemoryOp, type MemoryState } from './db';
import type { Sync } from './sync';
import { Button, color, Empty, Field, Group, Icon, MIN_TOUCH, Note, Segmented, styles, T } from './ui';

const TABS: { value: MemoryState; label: string }[] = [
  { value: 'proposed', label: 'Inbox' },
  { value: 'confirmed', label: 'Saved' },
  { value: 'archived', label: 'Archived' },
];
const ERRORS: Record<string, string> = {
  stale_revision: 'Changed on another device.',
  not_found: 'No longer exists on the server.',
  invalid_text: 'The server rejected this text (one line, no %% or <!--).',
};

// Extracted facts, decisions and ideas. Everything here reads the phone's cache, so search
// works offline; edits queue and sync like todos. Saved memories are written to the vault's
// Mobile/ notes by the Windows service.
export function Memories({ sync, version, query, onEdit }: { sync: Sync; version: number; query: string; onEdit: () => void }) {
  const db = useSQLiteContext() as unknown as Db;
  const [tab, setTab] = useState<MemoryState>('proposed');
  const [items, setItems] = useState<Memory[]>([]);
  const [open, setOpen] = useState<string | null>(null);

  const refresh = useCallback(async () => setItems(await listMemories(db, { query, state: tab })), [db, query, tab]);
  useEffect(() => { refresh(); }, [refresh, version]);

  const edit = async (m: Memory, op: MemoryOp) => {
    // Save/archive moves the row to another tab: drop it now, queue the write behind any sync.
    if (op.action === 'set_state' && op.payload.state !== m.state) setItems((list) => list.filter((x) => x.id !== m.id));
    try {
      await applyMemory(db, m.id, op, randomUUID);
    } catch (e) {
      Alert.alert('Could not save', String(e));
    }
    await refresh();
    onEdit();
  };
  const resolve = async (seq: number, choice: 'keep_mine' | 'discard') => {
    await sync.resolve(seq, choice);
    await refresh();
    onEdit();
  };

  return (
    <>
      <Segmented options={TABS} value={tab} onChange={(v) => { setTab(v); setOpen(null); }} />
      {items.length > 0 ? (
        <Group>
          {items.map((m) => (
            <MemoryRow key={m.id} m={m} expanded={open === m.id} onToggle={() => setOpen(open === m.id ? null : m.id)}
              onEdit={(op) => edit(m, op)} onResolve={resolve} />
          ))}
        </Group>
      ) : query ? (
        <Empty icon="magnifyingglass" title="No matches on this phone" detail="Search covers text, people, projects and dates." />
      ) : tab === 'proposed' ? (
        <Empty icon="tray" title="Inbox zero" detail="New facts, decisions and ideas from your conversations land here for review." />
      ) : (
        <Empty icon={tab === 'confirmed' ? 'bookmark' : 'archivebox'} title="None yet" />
      )}
    </>
  );
}

const SAVE: MemoryOp = { action: 'set_state', payload: { state: 'confirmed' } };
const SWIPE_THRESHOLD = 80;
const ARCHIVE: MemoryOp = { action: 'set_state', payload: { state: 'archived' } };

function MemoryRow({ m, expanded, onToggle, onEdit, onResolve }: { m: Memory; expanded: boolean; onToggle: () => void;
  onEdit: (op: MemoryOp) => void; onResolve: (seq: number, choice: 'keep_mine' | 'discard') => void }) {
  const [draft, setDraft] = useState<string | null>(null);
  const subject = m.entity_name ? `${m.kind} · ${m.entity_name}` : m.kind;
  const when = m.observed_at ? m.observed_at.slice(0, 10) : 'unknown date';
  const p = m.problem;
  const e = m.evidence;
  const swipeable = !p && draft === null;
  return (
    <Swipeable
      enabled={swipeable}
      friction={2}
      overshootLeft={false}
      overshootRight={false}
      // Swipe right saves (restores from Archived), swipe left archives; past the threshold the
      // action happens on release, no second tap.
      leftThreshold={SWIPE_THRESHOLD}
      rightThreshold={SWIPE_THRESHOLD}
      onSwipeableWillOpen={(direction) => onEdit(direction === 'right' ? SAVE : ARCHIVE)}
      renderLeftActions={m.state !== 'confirmed' ? () => (
        <SwipeAction icon="bookmark.fill" label={m.state === 'archived' ? 'Restore' : 'Save'} tone={color.tint} />
      ) : undefined}
      renderRightActions={m.state !== 'archived' ? () => (
        <SwipeAction icon="archivebox.fill" label="Archive" tone={color.neutral} />
      ) : undefined}
    >
      <View style={{ backgroundColor: color.cell }}>
        <Pressable onPress={onToggle} accessibilityRole="button" accessibilityState={{ expanded }}
          accessibilityHint={expanded ? 'Hides evidence' : 'Shows evidence'}
          style={({ pressed }) => pressed && { backgroundColor: color.fill }}>
          <View style={[styles.row, { alignItems: 'flex-start' }]}>
            <View style={{ flex: 1, gap: 3 }}>
              <T>{m.text}</T>
              <T ramp="footnote" tone={color.secondary}>
                {subject} · {when}{m.confidence !== null ? ` · confidence ${m.confidence}` : ''}
              </T>
            </View>
            {m.pending > 0 && !p && <Icon name="clock" size={15} tint={color.secondary} style={{ width: 15, height: 15, marginTop: 4 }} />}
            <Icon name={expanded ? 'chevron.up' : 'chevron.down'} size={13} tint={color.tertiary} weight="semibold"
              style={{ width: 13, height: 13, marginTop: 6 }} />
          </View>
        </Pressable>

        {(expanded || p || draft !== null) && (
          <View style={{ paddingHorizontal: 16, paddingBottom: 14, gap: 10 }}>
            {expanded && (
              <View style={{ gap: 6, paddingLeft: 12, borderLeftWidth: 1, borderLeftColor: color.separator }}>
                {e?.excerpt
                  ? <T ramp="callout">“{e.excerpt}”</T>
                  : <T ramp="callout" tone={color.secondary}>No excerpt available.</T>}
                <T ramp="footnote" tone={color.secondary}>
                  Recorded {e?.recorded_at ? new Date(e.recorded_at).toLocaleString() : 'at an unknown time'}
                  {e?.start !== undefined ? ` · ${e.start.toFixed(0)}–${e.end!.toFixed(0)} s` : ''}
                </T>
                {m.text !== m.original_text && <T ramp="footnote" tone={color.secondary}>Extracted as “{m.original_text}”</T>}
                {!!e?.note && <T ramp="footnote" tone={color.secondary} selectable>Vault: {e.note}</T>}
              </View>
            )}
            {p && (
              <>
                <Note icon="exclamationmark.triangle.fill" text={ERRORS[p.error] ?? p.error} />
                {p.server && <T ramp="footnote" tone={color.secondary}>On the server: “{p.server.text}”, {p.server.state}</T>}
                <View style={actions}>
                  {p.outcome === 'conflict' && <Button kind="tinted" title="Keep Mine" onPress={() => onResolve(p.seq, 'keep_mine')} />}
                  <Button kind="tinted" tone={color.danger} title="Discard Change" onPress={() => onResolve(p.seq, 'discard')} />
                </View>
              </>
            )}
            {!p && draft !== null && (
              <>
                <View style={{ backgroundColor: color.fill, borderRadius: 10, paddingHorizontal: 12 }}>
                  <Field value={draft} onChangeText={setDraft} accessibilityLabel="Corrected memory" autoFocus />
                </View>
                <View style={actions}>
                  <Button kind="filled" title="Save" disabled={!draft.trim() || draft.trim() === m.text}
                    onPress={() => { onEdit({ action: 'set_text', payload: { text: draft.trim() } }); setDraft(null); }} />
                  <Button kind="tinted" tone={color.secondary} title="Cancel" onPress={() => setDraft(null)} />
                </View>
              </>
            )}
            {!p && draft === null && expanded && (
              <View style={actions}>
                {m.state === 'proposed' && <Button kind="filled" icon="bookmark.fill" title="Save" onPress={() => onEdit(SAVE)} />}
                {m.state !== 'archived' && <Button kind="tinted" icon="pencil" title="Correct" onPress={() => setDraft(m.text)} />}
                {m.state !== 'archived'
                  ? <Button kind="tinted" tone={color.secondary} icon="archivebox" title="Archive" onPress={() => onEdit(ARCHIVE)} />
                  : <Button kind="tinted" icon="arrow.uturn.backward" title="Restore" onPress={() => onEdit(SAVE)} />}
              </View>
            )}
          </View>
        )}
      </View>
    </Swipeable>
  );
}

// Swipes are a shortcut; the expanded row's buttons stay the accessible path.
function SwipeAction({ icon, label, tone }: { icon: 'bookmark.fill' | 'archivebox.fill'; label: string; tone: typeof color.tint }) {
  return (
    <View accessibilityElementsHidden
      style={{ width: 88, minHeight: MIN_TOUCH, backgroundColor: tone, alignItems: 'center', justifyContent: 'center', gap: 2 }}>
      <Icon name={icon} tint={color.onTint} size={20} />
      <T ramp="caption1" tone={color.onTint} style={{ fontWeight: '600' }}>{label}</T>
    </View>
  );
}

const actions = { flexDirection: 'row', gap: 8, flexWrap: 'wrap' } as const;
