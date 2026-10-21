import { useEffect, useState } from 'react';
import { View } from 'react-native';
import type { Pipeline as Summary, Sync } from './sync';
import { color, Group, Icon, Note, Row, T, ValueRow } from './ui';

const STALE_MS = 5 * 60_000; // the worker reports every minute

// Shadow import from the Windows service: read-only, the vault and tray stay authoritative.
export function Pipeline({ sync, version }: { sync: Sync; version: number }) {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    sync.pipeline().then(
      (s) => live && (setSummary(s), setError(null)),
      () => live && setError('Unavailable while offline or not connected.'),
    );
    return () => { live = false; };
  }, [sync, version]);

  if (error && !summary) return <Group header="Windows pipeline" footer={error}>{null}</Group>;
  if (!summary) return null;
  const c = summary.counts;
  const waiting = summary.oldest_pending_command_at;
  return (
    <>
      <Group header="Windows pipeline" footer="Read-only preview. The vault and the Windows tray stay authoritative.">
        {summary.workers.length === 0 && (
          <Row><T tone={color.secondary}>No Windows worker registered.</T></Row>
        )}
        {summary.workers.map((w) => {
          const seen = w.last_seen_at ? new Date(w.last_seen_at) : null;
          const stale = !seen || Date.now() - seen.getTime() > STALE_MS;
          const jobs = w.status?.jobs ?? {};
          const detail = [jobs.pending ? `${jobs.pending} recordings queued` : '', jobs.failed ? `${jobs.failed} failed` : ''].filter(Boolean).join(' · ');
          return (
            <View key={w.name}>
              <Row>
                <Icon name="desktopcomputer" tint={stale ? color.warning : color.success} size={22} />
                <View style={{ flex: 1, gap: 2 }}>
                  <T>{w.name}</T>
                  <T ramp="footnote" tone={stale ? color.warning : color.secondary}>
                    {seen ? `Last report ${seen.toLocaleString()}` : 'Never reported'}{detail ? ` · ${detail}` : ''}
                  </T>
                </View>
              </Row>
              {!!w.status?.export_conflicts && (
                <View style={{ paddingLeft: 50, paddingRight: 16, paddingBottom: 12 }}>
                  <Note icon="doc.on.doc" text={`${w.status.export_conflicts} vault note(s) edited by hand; app changes were saved next to them in Mobile/`} />
                </View>
              )}
            </View>
          );
        })}
        {!!waiting && (
          <View style={{ paddingHorizontal: 16, paddingVertical: 11 }}>
            <Note icon="clock" tone={Date.now() - Date.parse(waiting) > STALE_MS ? color.warning : color.secondary}
              text={`Speaker changes waiting for Windows since ${new Date(waiting).toLocaleString()}`} />
          </View>
        )}
      </Group>
      <Group header="Imported">
        <ValueRow label="Conversations" value={String(c.source ?? 0)} />
        <ValueRow label="Extracted items" value={String(c.item ?? 0)} />
        <ValueRow label="People" value={String(c.person ?? 0)} />
        <ValueRow label="Speakers" value={String(c.observation ?? 0)} />
      </Group>
    </>
  );
}
