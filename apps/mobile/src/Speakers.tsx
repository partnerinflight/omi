import { randomUUID } from 'expo-crypto';
import { setAudioModeAsync, useAudioPlayer, useAudioPlayerStatus } from 'expo-audio';
import { useSQLiteContext } from 'expo-sqlite';
import { useCallback, useEffect, useState } from 'react';
import { Alert, StyleSheet, Text, TextInput, View } from 'react-native';
import { Button } from './Connection';
import { listCommands, queueCommand, type Command, type CommandPayload, type Db } from './db';
import type { Sync, WorkerRecord } from './sync';
import { MIN_TOUCH, useTheme } from './theme';

type Observation = { display: string; recorded_at: string; person_id: string | null; score: number | null;
  provenance: 'confirmed' | 'inferred' | 'cleared' | null; published: number | null; clips: number[] };
type Person = { name: string };
type Clarification = { text: string; questions: string[]; state: string };

const WAITING: Record<string, string> = {
  queued: 'Saved on this phone — sends when online',
  pending_worker: 'Saved — Windows applies it when it next checks in',
  failed_worker: 'Windows could not apply this',
  rejected: 'The server refused this change',
};

function target(p: CommandPayload) {
  return 'observation' in p ? p.observation : 'person' in p ? p.person : p.item;
}

// Speaker review and clarifications. The lists come from the Windows worker's last export and
// need a connection; edits queue offline and are applied on Windows in order.
export function Speakers({ sync, version, onEdit }: { sync: Sync; version: number; onEdit: () => void }) {
  const db = useSQLiteContext() as unknown as Db;
  const t = useTheme();
  const [obs, setObs] = useState<WorkerRecord<Observation>[]>([]);
  const [people, setPeople] = useState<WorkerRecord<Person>[]>([]);
  const [questions, setQuestions] = useState<WorkerRecord<Clarification>[]>([]);
  const [commands, setCommands] = useState<Map<string, Command>>(new Map());
  const [offline, setOffline] = useState(false);

  const load = useCallback(async () => {
    const latest = new Map<string, Command>();
    for (const c of await listCommands(db)) if (!latest.has(target(c.payload))) latest.set(target(c.payload), c);
    setCommands(latest);
    try {
      const [o, p, q] = await Promise.all([sync.records<Observation>('observation'), sync.records<Person>('person'),
        sync.records<Clarification>('clarification')]);
      setObs(o);
      setPeople(p);
      setQuestions(q);
      setOffline(false);
    } catch {
      setOffline(true); // keep the last lists; edits still queue
    }
  }, [db, sync]);
  useEffect(() => { load(); }, [load, version]);
  useEffect(() => { setAudioModeAsync({ playsInSilentMode: true, interruptionMode: 'doNotMix' }); }, []);

  const send = async (payload: CommandPayload) => {
    try {
      await queueCommand(db, payload, randomUUID);
    } catch (e) {
      Alert.alert('Could not save', String(e));
    }
    await load();
    onEdit();
  };

  // A target with a change in flight is locked: a second edit would carry a stale base hash.
  const status = (id: string) => {
    const c = commands.get(id);
    if (!c || c.state === 'applied_worker') return { busy: false, note: null };
    const busy = c.state === 'queued' || c.state === 'pending_worker';
    const text = `${WAITING[c.state]}${c.error ? `: ${c.error}` : ''}`;
    // A refused command stays in the outbox (and blocks nothing) until dismissed.
    const note = c.state === 'rejected' && c.seq !== null ? (
      <View style={styles.row}>
        <Text style={{ color: t.badge, flex: 1 }}>{text}</Text>
        <Button title="Dismiss" color={t.muted} onPress={() => sync.resolve(c.seq!, 'discard').then(load).then(onEdit)} />
      </View>
    ) : <Text style={{ color: t.badge }}>{text}</Text>;
    return { busy, note };
  };
  const name = (id: string | null) => people.find((p) => p.id === id)?.data.name;
  const toName = obs.filter((o) => o.data.provenance !== 'confirmed' && o.data.published !== 0 && o.data.clips.length > 0);
  const open = questions.filter((q) => q.data.state === 'pending');

  return (
    <View style={{ gap: 12 }}>
      {offline && <Text style={{ color: t.muted }}>Showing the last loaded lists; reconnect to refresh.</Text>}
      <Text style={{ color: t.text, fontWeight: '600' }}>Speakers to name ({toName.length})</Text>
      {toName.map((o) => {
        const s = status(o.id);
        const guess = o.data.provenance === 'inferred' ? name(o.data.person_id) : undefined;
        return (
          <View key={o.id} style={[styles.card, { borderColor: t.border, backgroundColor: t.card }]}>
            <Text style={{ color: t.text }}>{o.data.display} · {new Date(o.data.recorded_at).toLocaleString()}</Text>
            {guess && <Text style={{ color: t.muted }}>Voice match: {guess}{o.data.score !== null ? ` (${o.data.score})` : ''}</Text>}
            <View style={{ flexDirection: 'row', gap: 8, flexWrap: 'wrap' }}>
              {o.data.clips.map((n) => <Clip key={n} sync={sync} observation={o.id} n={n} />)}
            </View>
            {s.note}
            {!s.busy && (
              <>
                <NameInput people={people} placeholder={guess ? `Not ${guess}? Enter a name` : 'Who is this?'}
                  onSave={(person, newName) => send(person
                    ? { action: 'assign', observation: o.id, person, base_hash: o.hash }
                    : { action: 'assign', observation: o.id, name: newName, base_hash: o.hash })} />
                <View style={styles.row}>
                  {guess && <Button title={`Confirm ${guess}`} color={t.accent}
                    onPress={() => send({ action: 'assign', observation: o.id, person: o.data.person_id!, base_hash: o.hash })} />}
                  {guess && <Button title="Not them" color={t.muted} onPress={() => send({ action: 'clear', observation: o.id, base_hash: o.hash })} />}
                  <Button title="Discard clips" color={t.muted} onPress={() => send({ action: 'discard', observation: o.id, base_hash: o.hash })} />
                </View>
              </>
            )}
          </View>
        );
      })}

      <Text style={{ color: t.text, fontWeight: '600' }}>People ({people.length})</Text>
      {people.map((p) => {
        const s = status(p.id);
        return (
          <View key={p.id} style={[styles.card, { borderColor: t.border, backgroundColor: t.card }]}>
            <Text style={{ color: t.text }}>{p.data.name}</Text>
            {s.note}
            {!s.busy && (
              <>
                <NameInput people={[]} placeholder="Rename" onSave={(_, newName) =>
                  send({ action: 'rename', person: p.id, name: newName, base_hash: p.hash })} />
                <Button title="Forget this person" color={t.muted} onPress={() => Alert.alert(
                  `Forget ${p.data.name}?`, 'Their voice profile is removed on Windows. Existing notes are not changed.',
                  [{ text: 'Cancel', style: 'cancel' },
                   { text: 'Forget', style: 'destructive', onPress: () => send({ action: 'forget', person: p.id, base_hash: p.hash }) }])} />
              </>
            )}
          </View>
        );
      })}

      <Text style={{ color: t.text, fontWeight: '600' }}>Clarifications ({open.length})</Text>
      {open.map((q) => {
        const s = status(q.id);
        return (
          <View key={q.id} style={[styles.card, { borderColor: t.border, backgroundColor: t.card }]}>
            {q.data.questions.map((text) => <Text key={text} style={{ color: t.text }}>{text}</Text>)}
            <Text style={{ color: t.muted }}>{q.data.text}</Text>
            {s.note}
            {!s.busy && (
              <>
                <NameInput people={[]} placeholder="Corrected sentence" initial={q.data.text}
                  onSave={(_, answer) => send({ action: 'clarify', item: q.id, answer })} />
                <Button title="Keep as is" color={t.muted} onPress={() => send({ action: 'clarify_dismiss', item: q.id })} />
              </>
            )}
          </View>
        );
      })}
    </View>
  );
}

// Typing an existing person's name (any case) assigns that person rather than creating a duplicate.
function NameInput({ people, placeholder, initial = '', onSave }: { people: WorkerRecord<Person>[]; placeholder: string;
  initial?: string; onSave: (person: string | null, name: string) => void }) {
  const t = useTheme();
  const [value, setValue] = useState(initial);
  const save = () => {
    const v = value.trim();
    if (!v) return;
    const match = people.find((p) => p.data.name.toLocaleLowerCase() === v.toLocaleLowerCase());
    onSave(match?.id ?? null, v);
  };
  return (
    <View style={styles.row}>
      <TextInput value={value} onChangeText={setValue} onSubmitEditing={save} placeholder={placeholder}
        placeholderTextColor={t.muted} accessibilityLabel={placeholder} autoCorrect={false}
        style={[styles.input, { color: t.text, borderColor: t.border }]} />
      <Button title="Save" color={t.accent} disabled={!value.trim()} onPress={save} />
    </View>
  );
}

function Clip({ sync, observation, n }: { sync: Sync; observation: string; n: number }) {
  const t = useTheme();
  const player = useAudioPlayer(null);
  const status = useAudioPlayerStatus(player);
  const [loaded, setLoaded] = useState(false);
  const toggle = async () => {
    if (status.playing) return player.pause();
    if (!loaded) {
      try {
        player.replace(await sync.clipSource(observation, n));
        setLoaded(true);
      } catch {
        return Alert.alert('Clip unavailable', 'Connect to the server to play clips.');
      }
    } else if (status.didJustFinish || status.currentTime >= status.duration) {
      player.seekTo(0);
    }
    player.play();
  };
  return <Button title={status.playing ? `Pause ${n + 1}` : `Play clip ${n + 1}`} color={t.accent} onPress={toggle} />;
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', gap: 12, flexWrap: 'wrap' },
  card: { borderWidth: StyleSheet.hairlineWidth, borderRadius: 10, padding: 12, gap: 6 },
  input: { flex: 1, minWidth: 160, minHeight: MIN_TOUCH, borderWidth: StyleSheet.hairlineWidth, borderRadius: 10, paddingHorizontal: 12, fontSize: 17 },
});
