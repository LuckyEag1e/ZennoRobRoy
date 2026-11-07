const { test } = require('node:test');
const assert = require('node:assert/strict');
const { makeClubProfile } = require('./helpers');
const { computeMemberStats, resolveTrainerName } = require('../src/memberStats');

test('computes avg/gain/latestGain/days per member and sorts by avg descending', () => {
  const data = makeClubProfile({
    members: [
      { id: 1, name: 'Slow', history: [[1, 100, 100], [2, 200, 100]] },
      { id: 2, name: 'Fast', history: [[1, 500, 500], [2, 1200, 700]] },
    ],
  });

  const { members, day, periodGain } = computeMemberStats(data);

  assert.deepEqual(
    members.map((m) => m.name),
    ['Fast', 'Slow'],
  );
  assert.deepEqual(members[0], { id: 2, name: 'Fast', avg: 600, gain: 1200, latestGain: 700, days: 2 });
  assert.equal(day, 2);
  assert.equal(periodGain, 1400);
});

test('uses the latest row even when history arrives out of order', () => {
  const data = makeClubProfile({
    members: [{ id: 1, name: 'A', history: [[3, 300, 50], [1, 100, 100], [2, 250, 150]] }],
  });
  const [m] = computeMemberStats(data).members;
  assert.equal(m.gain, 300);
  assert.equal(m.latestGain, 50);
  assert.equal(m.days, 3);
});

test('a member who joined mid-period averages over their own days only', () => {
  const data = makeClubProfile({
    members: [
      { id: 1, name: 'Veteran', history: [[1, 100], [2, 200], [3, 300], [4, 400]] },
      { id: 2, name: 'Newcomer', history: [[3, 150], [4, 300]] },
    ],
  });
  const { members } = computeMemberStats(data);
  const newcomer = members.find((m) => m.name === 'Newcomer');
  assert.equal(newcomer.days, 2);
  assert.equal(newcomer.avg, 150);
});

test('ignores past members who are no longer in circle_user_array', () => {
  const data = makeClubProfile({
    members: [{ id: 1, name: 'Current', history: [[1, 100]] }],
    pastMembers: [{ id: 99, name: 'Left', history: [[1, 99999]] }],
  });
  const { members, periodGain } = computeMemberStats(data);
  assert.deepEqual(
    members.map((m) => m.name),
    ['Current'],
  );
  assert.equal(periodGain, 100);
});

test('current members with no history get a zero row instead of being dropped', () => {
  const data = makeClubProfile({ members: [{ id: 7, name: 'Quiet', history: [] }] });
  assert.deepEqual(computeMemberStats(data).members, [
    { id: 7, name: 'Quiet', avg: 0, gain: 0, latestGain: 0, days: 0 },
  ]);
});

test('returns an empty result when the response has no club', () => {
  assert.deepEqual(computeMemberStats({}), { members: [], day: 0, periodGain: 0 });
});

test('resolveTrainerName matches string or numeric ids on the current roster only', () => {
  const data = makeClubProfile({
    members: [{ id: 123, name: 'Teio' }],
    pastMembers: [{ id: 456, name: 'Gone' }],
  });
  assert.equal(resolveTrainerName(data, 123), 'Teio');
  // trainerLinks stores ids as strings
  assert.equal(resolveTrainerName(data, '123'), 'Teio');
  assert.equal(resolveTrainerName(data, '456'), null);
  assert.equal(resolveTrainerName({}, '123'), null);
});
