import { pairRoute } from '../pairLink';

// Deep links from the server's pairing QR code; every other link is left as is.
export function redirectSystemPath({ path }: { path: string; initial: boolean }) {
  return pairRoute(path) ?? path;
}
