import { useEffect, useState } from 'react';
import { Text, View } from 'react-native';
import type { Pipeline as Summary, Sync } from './sync';
import { useTheme } from './theme';

const STALE_MS = 5 * 60_000; // the worker reports every minute

// Shadow import from the Windows service: read-only, the vault and tray stay authoritative.
export function Pipeline({ sync, version }: { sync: Sync; version: number }) {
  const t = useTheme();
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

  if (error && !summary) return <Text style={{ color: t.muted }}>{error}</Text>;
  if (!summary) return null;
  const c = summary.counts;
  return (
    <View style={{ gap: 4 }}>
      {summary.workers.length === 0 && <Text style={{ color: t.muted }}>No Windows worker registered.</Text>}
      {summary.workers.map((w) => {
        const seen = w.last_seen_at ? new Date(w.last_seen_at) : null;
        const stale = !seen || Date.now() - seen.getTime() > STALE_MS;
        const jobs = w.status?.jobs ?? {};
        return (
          <Text key={w.name} style={{ color: stale ? t.badge : t.text }}>
            {w.name}: {seen ? `last report ${seen.toLocaleString()}` : 'never reported'}
            {jobs.pending ? ` · ${jobs.pending} recordings queued` : ''}
            {jobs.failed ? ` · ${jobs.failed} failed` : ''}
            {w.status?.export_conflicts ? ` · ${w.status.export_conflicts} vault note(s) edited by hand; app changes were saved next to them in Mobile/` : ''}
          </Text>
        );
      })}
      <Text style={{ color: t.muted }}>
        {c.source ?? 0} conversations · {c.item ?? 0} extracted items · {c.person ?? 0} people · {c.observation ?? 0} speakers
      </Text>
    </View>
  );
}
