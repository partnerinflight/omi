import { randomUUID } from 'expo-crypto';
import { setAudioModeAsync, useAudioPlayer, useAudioPlayerStatus } from 'expo-audio';
import { useSQLiteContext } from 'expo-sqlite';
import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { Alert, Pressable, View } from 'react-native';
import { listCommands, queueCommand, type Command, type CommandPayload, type Db } from './db';
import type { Sync, WorkerRecord } from './sync';
import { Button, color, Empty, Field, Group, Icon, Note, Row, styles, T } from './ui';

type Observation = { display: string; recorded_at: string; person_id: string | null; score: number | null;
  provenance: 'confirmed' | 'inferred' | 'cleared' | null; published: number | null; clips: number[] };
type Person = { name: string };
type Clarification = { text: string; questions: string[]; state: string; context?: string; note?: string };

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
  const [obs, setObs] = useState<WorkerRecord<Observation>[]>([]);
  const [people, setPeople] = useState<WorkerRecord<Person>[]>([]);
  const [questions, setQuestions] = useState<WorkerRecord<Clarification>[]>([]);
  const [commands, setCommands] = useState<Map<string, Command>>(new Map());
  const [offline, setOffline] = useState(false);
  const [loaded, setLoaded] = useState(false);

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
    setLoaded(true);
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
  const status = (id: string): { busy: boolean; note: ReactNode } => {
    const c = commands.get(id);
    if (!c || c.state === 'applied_worker') return { busy: false, note: null };
    const busy = c.state === 'queued' || c.state === 'pending_worker';
    const text = `${WAITING[c.state]}${c.error ? `: ${c.error}` : ''}`;
    const tone = busy ? color.secondary : color.warning;
    const icon = busy ? 'clock' : 'exclamationmark.triangle.fill';
    // A refused command stays in the outbox (and blocks nothing) until dismissed.
    const note = (
      <View style={{ gap: 8 }}>
        <Note icon={icon} text={text} tone={tone} />
        {c.state === 'rejected' && c.seq !== null && (
          <View style={actions}>
            <Button kind="tinted" tone={color.secondary} title="Dismiss" onPress={() => sync.resolve(c.seq!, 'discard').then(load).then(onEdit)} />
          </View>
        )}
      </View>
    );
    return { busy, note };
  };
  const name = (id: string | null) => people.find((p) => p.id === id)?.data.name;
  // Windows assigns voice matches itself (they name speakers in notes), as the tray shows them:
  // only speakers with no person need naming; matches are listed so a wrong one can be corrected.
  const reviewable = (o: WorkerRecord<Observation>) => o.data.published !== 0 && o.data.clips.length > 0;
  const toName = obs.filter((o) => !o.data.person_id && reviewable(o));
  const matched = obs.filter((o) => o.data.provenance === 'inferred' && o.data.person_id && reviewable(o));
  const open = questions.filter((q) => q.data.state === 'pending');
  const assign = (o: WorkerRecord<Observation>) => (person: string | null, newName: string) => send(person
    ? { action: 'assign', observation: o.id, person, base_hash: o.hash }
    : { action: 'assign', observation: o.id, name: newName, base_hash: o.hash });

  if (!loaded) return null;
  if (offline && obs.length === 0 && people.length === 0 && questions.length === 0) {
    return <Empty icon="wifi.slash" title="Not connected" detail="Speaker review needs the server. Pair this phone on the Status tab." />;
  }

  return (
    <>
      {offline && <Note icon="wifi.slash" tone={color.secondary} text="Showing the last loaded lists. Reconnect to refresh." />}

      <Group header={`To name · ${toName.length}`} footer={toName.length ? 'Play a clip, then type who it is.' : undefined}>
        {toName.map((o) => {
          const s = status(o.id);
          return (
            <Item key={o.id}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                <Avatar />
                <View style={{ flex: 1 }}>
                  <T ramp="headline">{o.data.display}</T>
                  <T ramp="footnote" tone={color.secondary}>{new Date(o.data.recorded_at).toLocaleString()}</T>
                </View>
              </View>
              <View style={actions}>
                {o.data.clips.map((n) => <Clip key={n} sync={sync} observation={o.id} n={n} />)}
              </View>
              {s.note}
              {!s.busy && (
                <>
                  <NameInput people={people} placeholder="Who is this?" onSave={assign(o)} />
                  <View style={actions}>
                    <Button kind="plain" tone={color.danger} title="Discard Clips"
                      onPress={() => send({ action: 'discard', observation: o.id, base_hash: o.hash })} />
                  </View>
                </>
              )}
            </Item>
          );
        })}
      </Group>
      {toName.length === 0 && <Empty icon="person.wave.2" title="Everyone is identified" detail="New unknown voices from your recordings appear here." />}

      {matched.length > 0 && (
        <Group header={`Voice matched · ${matched.length}`} inset={64}
          footer="Named automatically from the voices you confirmed. Tap one to correct it.">
          {matched.map((o) => (
            <MatchedRow key={o.id} o={o} name={name(o.data.person_id) ?? 'Unknown person'} note={status(o.id)}
              people={people} sync={sync} onAssign={assign(o)}
              onClear={() => send({ action: 'clear', observation: o.id, base_hash: o.hash })} />
          ))}
        </Group>
      )}

      {open.length > 0 && (
        <Group header={`Clarifications · ${open.length}`}
          footer="Add missing names or context so these notes make sense on their own.">
          {open.map((q) => {
            const s = status(q.id);
            return (
              <ClarificationCard key={q.id} q={q} note={s}
                onSave={(answer) => send({ action: 'clarify', item: q.id, answer })}
                onKeep={() => send({ action: 'clarify_dismiss', item: q.id })} />
            );
          })}
        </Group>
      )}

      {people.length > 0 && (
        <Group header={`People · ${people.length}`} inset={64}>
          {people.map((p) => <PersonRow key={p.id} person={p} note={status(p.id)} send={send} />)}
        </Group>
      )}
    </>
  );
}

// Mirrors the tray's clarification page: the question, the note line as written, the
// conversation it came from, and a corrected sentence that replaces that one line in the vault.
function ClarificationCard({ q, note, onSave, onKeep }: { q: WorkerRecord<Clarification>; note: { busy: boolean; note: ReactNode };
  onSave: (answer: string) => void; onKeep: () => void }) {
  const [draft, setDraft] = useState(q.data.text);
  const [showSource, setShowSource] = useState(false);
  const answer = draft.replace(/\s+/g, ' ').trim(); // the note line is one line; Windows rejects line breaks
  const changed = answer.length > 0 && answer !== q.data.text;
  return (
    <Item>
      {q.data.questions.map((text) => <T key={text} ramp="headline">{text}</T>)}
      <View style={{ gap: 4 }}>
        <T ramp="footnote" tone={color.secondary}>In {q.data.note ?? 'the note'}</T>
        <T ramp="callout" tone={color.secondary}>“{q.data.text}”</T>
      </View>
      {!!q.data.context && (
        <View style={{ gap: 6 }}>
          <Pressable onPress={() => setShowSource(!showSource)} accessibilityRole="button" accessibilityState={{ expanded: showSource }}
            hitSlop={8} style={{ flexDirection: 'row', alignItems: 'center', gap: 6, alignSelf: 'flex-start', minHeight: 32 }}>
            <Icon name={showSource ? 'chevron.down' : 'chevron.right'} size={12} weight="semibold" />
            <T ramp="subheadline" tone={color.tint} style={{ fontWeight: '600' }}>Source excerpt</T>
          </Pressable>
          {showSource && (
            <View style={{ paddingLeft: 12, borderLeftWidth: 1, borderLeftColor: color.separator }}>
              <T ramp="callout" selectable>{q.data.context}</T>
            </View>
          )}
        </View>
      )}
      {note.note}
      {!note.busy && (
        <>
          <View style={{ gap: 6 }}>
            <T ramp="footnote" tone={color.secondary}>Corrected note — include the date and the missing context</T>
            <View style={{ backgroundColor: color.fill, borderRadius: 10, paddingHorizontal: 12, paddingVertical: 4 }}>
              <Field value={draft} onChangeText={setDraft} multiline maxLength={4000} submitBehavior="blurAndSubmit" returnKeyType="done"
                onSubmitEditing={() => changed && onSave(answer)} accessibilityLabel="Corrected note"
                style={{ paddingTop: 8, paddingBottom: 8, textAlignVertical: 'top' }} />
            </View>
          </View>
          <View style={actions}>
            <Button kind="filled" title="Save Corrected Note" disabled={!changed} onPress={() => onSave(answer)} />
            <Button kind="tinted" tone={color.secondary} title="Keep as Is" onPress={onKeep} />
          </View>
        </>
      )}
    </Item>
  );
}

function MatchedRow({ o, name, note, people, sync, onAssign, onClear }: { o: WorkerRecord<Observation>; name: string;
  note: { busy: boolean; note: ReactNode }; people: WorkerRecord<Person>[]; sync: Sync;
  onAssign: (person: string | null, name: string) => void; onClear: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <View>
      <Row onPress={() => setOpen(!open)} accessibilityHint={open ? 'Hides clips and corrections' : 'Shows clips and corrections'}>
        <Avatar name={name} />
        <View style={{ flex: 1, gap: 2 }}>
          <T>{name}</T>
          <T ramp="footnote" tone={color.secondary}>
            {o.data.display} · {new Date(o.data.recorded_at).toLocaleDateString()}{o.data.score !== null ? ` · match ${o.data.score.toFixed(2)}` : ''}
          </T>
        </View>
        <Icon name={open ? 'chevron.up' : 'chevron.down'} size={13} tint={color.tertiary} weight="semibold" style={{ width: 13, height: 13 }} />
      </Row>
      {(open || note.note) && (
        <View style={{ paddingLeft: 64, paddingRight: 16, paddingBottom: 14, gap: 10 }}>
          {note.note}
          {open && !note.busy && (
            <>
              <View style={actions}>
                {o.data.clips.map((n) => <Clip key={n} sync={sync} observation={o.id} n={n} />)}
              </View>
              <NameInput people={people} placeholder="Wrong person? Type the right name" onSave={onAssign} />
              <View style={actions}>
                <Button kind="plain" tone={color.danger} title={`Not ${name}`} onPress={onClear} />
              </View>
            </>
          )}
        </View>
      )}
    </View>
  );
}

function PersonRow({ person: p, note, send }: { person: WorkerRecord<Person>; note: { busy: boolean; note: ReactNode };
  send: (payload: CommandPayload) => void }) {
  const [editing, setEditing] = useState(false);
  const forget = () => Alert.alert(
    `Forget ${p.data.name}?`, 'Their voice profile is removed on Windows. Existing notes are not changed.',
    [{ text: 'Cancel', style: 'cancel' },
     { text: 'Forget', style: 'destructive', onPress: () => send({ action: 'forget', person: p.id, base_hash: p.hash }) }]);
  return (
    <View>
      <Row>
        <Avatar name={p.data.name} />
        <T style={{ flex: 1 }}>{p.data.name}</T>
        {!note.busy && !editing && (
          <>
            <Button kind="plain" title="Rename" onPress={() => setEditing(true)} />
            <Button kind="plain" tone={color.danger} title="Forget" onPress={forget} accessibilityLabel={`Forget ${p.data.name}`} />
          </>
        )}
      </Row>
      {(note.note || editing) && (
        <View style={{ paddingLeft: 64, paddingRight: 16, paddingBottom: 12, gap: 8 }}>
          {note.note}
          {editing && !note.busy && (
            <NameInput people={[]} placeholder="New name" initial={p.data.name} autoFocus onCancel={() => setEditing(false)}
              onSave={(_, newName) => { setEditing(false); send({ action: 'rename', person: p.id, name: newName, base_hash: p.hash }); }} />
          )}
        </View>
      )}
    </View>
  );
}

function Item({ children }: { children: ReactNode }) {
  return <View style={{ padding: 16, gap: 12 }}>{children}</View>;
}

function Avatar({ name }: { name?: string }) {
  const initials = name?.split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toLocaleUpperCase();
  return (
    <View style={{ width: 36, height: 36, borderRadius: 18, backgroundColor: color.fill, alignItems: 'center', justifyContent: 'center' }}
      importantForAccessibility="no-hide-descendants" accessibilityElementsHidden>
      {initials ? <T ramp="subheadline" tone={color.secondary} style={{ fontWeight: '600' }}>{initials}</T>
        : <Icon name="person.fill.questionmark" size={18} tint={color.secondary} />}
    </View>
  );
}

// Typing an existing person's name (any case) assigns that person rather than creating a duplicate.
function NameInput({ people, placeholder, initial = '', autoFocus, onSave, onCancel }: { people: WorkerRecord<Person>[]; placeholder: string;
  initial?: string; autoFocus?: boolean; onSave: (person: string | null, name: string) => void; onCancel?: () => void }) {
  const [value, setValue] = useState(initial);
  const save = () => {
    const v = value.trim();
    if (!v) return;
    const match = people.find((p) => p.data.name.toLocaleLowerCase() === v.toLocaleLowerCase());
    onSave(match?.id ?? null, v);
  };
  return (
    <View style={[styles.row, { paddingHorizontal: 12, paddingVertical: 0, backgroundColor: color.fill, borderRadius: 10 }]}>
      <Field value={value} onChangeText={setValue} onSubmitEditing={save} placeholder={placeholder} autoFocus={autoFocus}
        accessibilityLabel={placeholder} autoCorrect={false} returnKeyType="done" />
      {onCancel && <Button kind="plain" tone={color.secondary} title="Cancel" onPress={onCancel} />}
      <Button kind="plain" title="Save" disabled={!value.trim()} onPress={save} />
    </View>
  );
}

function Clip({ sync, observation, n }: { sync: Sync; observation: string; n: number }) {
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
  return <Button kind="tinted" icon={status.playing ? 'pause.fill' : 'play.fill'} title={`Clip ${n + 1}`} onPress={toggle}
    accessibilityLabel={status.playing ? `Pause clip ${n + 1}` : `Play clip ${n + 1}`} />;
}

const actions = { flexDirection: 'row', gap: 8, flexWrap: 'wrap', alignItems: 'center' } as const;
