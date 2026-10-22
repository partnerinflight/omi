// The QR on the server's /status page holds secondbrain://pair?server=…&code=…. The phone camera
// opens the app with it; +native-intent sends it to the Status tab with both fields prefilled,
// and nothing pairs until the user taps Pair (they see which server first).
import { serverUrlProblem } from './sync.ts';

export function parsePairLink(link: string): { server: string; code: string } | null {
  const m = /^(?:secondbrain:\/\/\/?|\/)?pair\/?\?(.*)$/.exec(link);
  if (!m) return null;
  const query = new URLSearchParams(m[1]);
  const server = query.get('server') ?? '';
  const code = query.get('code') ?? '';
  return serverUrlProblem(server) || !/^[A-Za-z0-9]{4,64}$/.test(code) ? null : { server, code };
}

export function pairRoute(link: string): string | null {
  const pair = parsePairLink(link);
  return pair && `/status?${new URLSearchParams(pair)}`;
}
