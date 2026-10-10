import * as SecureStore from 'expo-secure-store';
import type { Tokens, TokenStore } from './sync';

const KEY = 'sync_tokens';
// Not restored onto another device from a backup: credentials are device-scoped.
const OPTS = { keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY };

export const keychainTokens: TokenStore = {
  async get() {
    const raw = await SecureStore.getItemAsync(KEY, OPTS);
    return raw ? (JSON.parse(raw) as Tokens) : null;
  },
  set: (tokens) => SecureStore.setItemAsync(KEY, JSON.stringify(tokens), OPTS),
  clear: () => SecureStore.deleteItemAsync(KEY, OPTS),
};
