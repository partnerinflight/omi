// Pairing QR deep links: `npm test`.
import assert from 'node:assert/strict';
import { pairRoute, parsePairLink } from '../src/pairLink.ts';

const link = 'secondbrain://pair?server=https%3A%2F%2Fbrain.example.com&code=ABCDEFGHJK';
assert.deepEqual(parsePairLink(link), { server: 'https://brain.example.com', code: 'ABCDEFGHJK' });
assert.deepEqual(parsePairLink('/pair?server=https%3A%2F%2Fbrain.example.com&code=ABCDEFGHJK'),
  { server: 'https://brain.example.com', code: 'ABCDEFGHJK' });
assert.equal(pairRoute(link), '/status?server=https%3A%2F%2Fbrain.example.com&code=ABCDEFGHJK');
// Anything that is not a well-formed https pairing link passes through untouched.
for (const bad of [
  'secondbrain://pair?server=http%3A%2F%2Fbrain.example.com&code=ABCDEFGHJK',
  'secondbrain://pair?server=https%3A%2F%2Fbrain.example.com%2Fx&code=ABCDEFGHJK',
  'secondbrain://pair?server=https%3A%2F%2Fbrain.example.com&code=AB%20CD',
  'secondbrain://pair?code=ABCDEFGHJK',
  'secondbrain://status?server=https%3A%2F%2Fbrain.example.com&code=ABCDEFGHJK',
]) assert.equal(pairRoute(bad), null, bad);
console.log('pairLink.test.ts: ok');
