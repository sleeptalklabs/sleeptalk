import test from 'node:test';
import assert from 'node:assert/strict';
import { pollSchedule, retryBackoff } from '../src/timing.js';

test('unchanged status backs off to ten seconds, progress resets to two', () => {
  let state;
  const observed = [];
  for (let i = 0; i < 6; i++) { state = pollSchedule(state, 'unchanged'); observed.push(state.delay); }
  assert.deepEqual(observed, [2000, 4000, 8000, 10000, 10000, 10000]);
  assert.equal(pollSchedule(state, 'saved').delay, 2000);
});
test('only two retransmissions have a bounded backoff', () => {
  assert.equal(retryBackoff(0), null);
  assert.equal(retryBackoff(1), 2000);
  assert.equal(retryBackoff(2), 4000);
  assert.equal(retryBackoff(3), null);
  assert.equal(retryBackoff(20), null);
});
