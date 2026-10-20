import { randomUUID } from 'expo-crypto';
import { useSQLiteContext } from 'expo-sqlite';
import { useCallback, useEffect, useState } from 'react';
import { Alert, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { Button } from './Connection';
import { applyMemory, listMemories, type Db, type Memory, type MemoryOp, type MemoryState } from './db';
import type { Sync } from './sync';
import { MIN_TOUCH, useTheme } from './theme';

const TABS: { state: MemoryState; label: string }[] = [
  { state: 'proposed', label: 'Inbox' },
  { state: 'confirmed', label: 'Saved' },
  { state: 'archived', label: 'Archived' },
];
const ERRORS: Record<string, string> = {
  stale_revision: 'Changed on another device.',
  not_found: 'No longer exists on the server.',
  invalid_text: 'The server rejected this text (one line, no %% or <!--).',
};

// Extracted facts, decisions and ideas. Everything here reads the phone's cache, so search
// works offline; edits queue and sync like todos. Saved memories are written to the vault's
// Mobile/ notes by the Windows service.
export function Memories({ sync, version, onEdit }: { sync: Sync; version: number; onEdit: () => void }) {
  const db = useSQLiteContext() as unknown as Db;
  const t = useTheme();
  const [tab, setTab] = useState<MemoryState>('proposed');
  const [query, setQuery] = useState('');
  const [items, setItems] = useState<Memory[]>([]);
  const [open, setOpen] = useState<string | null>(null);

  const refresh = useCallback(async () => setItems(await listMemories(db, { query, state: tab })), [db, query, tab]);
  useEffect(() => { refresh(); }, [refresh, version]);

  const edit = async (m: Memory, op: MemoryOp) => {
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
    <View style={{ gap: 8 }}>
      <TextInput value={query} onChangeText={setQuery} placeholder="Search text, person, project or date"
        placeholderTextColor={t.muted} accessibilityLabel="Search memories" autoCorrect={false} clearButtonMode="while-editing"
        style={[styles.input, { color: t.text, borderColor: t.border, backgroundColor: t.card }]} />
      <View style={{ flexDirection: 'row', gap: 8 }} accessibilityRole="tablist">
        {TABS.map((x) => (
          <Pressable key={x.state} onPress={() => setTab(x.state)} accessibilityRole="tab" accessibilityState={{ selected: tab === x.state }}
            style={[styles.tab, { borderColor: tab === x.state ? t.accent : t.border }]}>
            <Text style={{ color: tab === x.state ? t.accent : t.text }}>{x.label}</Text>
          </Pressable>
        ))}
      </View>
      {items.length === 0 && (
        <Text style={{ color: t.muted }}>{query ? 'No matches on this phone.' : tab === 'proposed' ? 'Nothing to review.' : 'None yet.'}</Text>
      )}
      {items.map((m) => (
        <MemoryCard key={m.id} m={m} expanded={open === m.id} onToggle={() => setOpen(open === m.id ? null : m.id)}
          onEdit={(op) => edit(m, op)} onResolve={resolve} />
      ))}
    </View>
  );
}

function MemoryCard({ m, expanded, onToggle, onEdit, onResolve }: { m: Memory; expanded: boolean; onToggle: () => void;
  onEdit: (op: MemoryOp) => void; onResolve: (seq: number, choice: 'keep_mine' | 'discard') => void }) {
  const t = useTheme();
  const [draft, setDraft] = useState<string | null>(null);
  const subject = m.entity_name ? `${m.kind} · ${m.entity_name}` : m.kind;
  const when = m.observed_at ? m.observed_at.slice(0, 10) : 'unknown date';
  const p = m.problem;
  const e = m.evidence;
  return (
    <View style={[styles.card, { backgroundColor: t.card, borderColor: p ? t.badge : t.border }]}>
      <Pressable onPress={onToggle} accessibilityRole="button" accessibilityHint={expanded ? 'Hides evidence' : 'Shows evidence'}
        style={{ minHeight: MIN_TOUCH, justifyContent: 'center' }}>
        <Text style={{ color: t.text }}>{m.text}</Text>
        <Text style={{ color: t.muted, fontSize: 13 }}>
          {subject} · {when}{m.confidence !== null ? ` · confidence ${m.confidence}` : ''}{m.pending && !p ? ' · Queued' : ''}
        </Text>
      </Pressable>
      {expanded && (
        <View style={{ gap: 4 }}>
          {m.text !== m.original_text && <Text style={{ color: t.muted }}>Extracted as: “{m.original_text}”</Text>}
          <Text style={{ color: t.muted }}>
            Recorded {e?.recorded_at ? new Date(e.recorded_at).toLocaleString() : 'at an unknown time'}
            {e?.start !== undefined ? ` · ${e.start.toFixed(0)}–${e.end!.toFixed(0)} s` : ''}
          </Text>
          {e?.excerpt ? <Text style={{ color: t.text }}>“{e.excerpt}”</Text> : <Text style={{ color: t.muted }}>No excerpt available.</Text>}
          {e?.note && <Text style={{ color: t.muted }} selectable>Vault: {e.note}</Text>}
        </View>
      )}
      {p && (
        <View style={{ gap: 4 }}>
          <Text style={{ color: t.badge }}>{ERRORS[p.error] ?? p.error}</Text>
          {p.server && <Text style={{ color: t.muted }}>Server: “{p.server.text}”, {p.server.state}</Text>}
          <View style={styles.row}>
            {p.outcome === 'conflict' && <Button title="Keep mine" color={t.accent} onPress={() => onResolve(p.seq, 'keep_mine')} />}
            <Button title="Discard this change" color={t.muted} onPress={() => onResolve(p.seq, 'discard')} />
          </View>
        </View>
      )}
      {!p && draft !== null && (
        <View style={styles.row}>
          <TextInput value={draft} onChangeText={setDraft} accessibilityLabel="Corrected memory" autoFocus multiline={false}
            style={[styles.input, { flex: 1, color: t.text, borderColor: t.border }]} />
          <Button title="Save" color={t.accent} disabled={!draft.trim() || draft.trim() === m.text}
            onPress={() => { onEdit({ action: 'set_text', payload: { text: draft.trim() } }); setDraft(null); }} />
          <Button title="Cancel" color={t.muted} onPress={() => setDraft(null)} />
        </View>
      )}
      {!p && draft === null && (
        <View style={styles.row}>
          {m.state === 'proposed' && <Button title="Save" color={t.accent} onPress={() => onEdit({ action: 'set_state', payload: { state: 'confirmed' } })} />}
          {m.state !== 'archived' && <Button title="Correct" color={t.accent} onPress={() => setDraft(m.text)} />}
          {m.state !== 'archived'
            ? <Button title="Archive" color={t.muted} onPress={() => onEdit({ action: 'set_state', payload: { state: 'archived' } })} />
            : <Button title="Restore" color={t.accent} onPress={() => onEdit({ action: 'set_state', payload: { state: 'confirmed' } })} />}
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', gap: 16, flexWrap: 'wrap' },
  card: { borderWidth: StyleSheet.hairlineWidth, borderRadius: 10, paddingHorizontal: 12, paddingBottom: 8 },
  input: { minHeight: MIN_TOUCH, borderWidth: StyleSheet.hairlineWidth, borderRadius: 10, paddingHorizontal: 12, fontSize: 17 },
  tab: { minHeight: MIN_TOUCH, paddingHorizontal: 12, justifyContent: 'center', borderWidth: 1, borderRadius: 10 },
});
