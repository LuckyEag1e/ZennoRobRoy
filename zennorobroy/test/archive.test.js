const { test, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const { dataDir, resetDataDir, makeClubProfile } = require('./helpers');
const { archiveSnapshot, readHistory, readMemberHistory } = require('../src/archive');

beforeEach(resetDataDir);

test('rejects non-numeric ids before touching the filesystem (path traversal guard)', () => {
  const evil = '../../../../tmp/zenno-pwned';
  assert.throws(() => archiveSnapshot(evil, makeClubProfile()), /Invalid circle_id/);
  assert.throws(() => readHistory(evil), /Invalid circle_id/);
  assert.throws(() => readMemberHistory('1', evil), /Invalid friend_viewer_id/);
  assert.throws(() => readHistory('12a'), /Invalid circle_id/);
});

test('writes a raw snapshot and one history line per fetch', () => {
  archiveSnapshot('42', makeClubProfile({ club: { fan_count: 5 } }));

  assert.equal(fs.readdirSync(path.join(dataDir, 'raw', '42')).length, 1);
  const history = readHistory('42');
  assert.equal(history.length, 1);
  assert.equal(history[0].fan_count, 5);
});

test('skips the history append when nothing but the timestamp changed', () => {
  archiveSnapshot('42', makeClubProfile({ club: { fan_count: 5 } }));
  archiveSnapshot('42', makeClubProfile({ club: { fan_count: 5 } }));
  assert.equal(readHistory('42').length, 1);

  archiveSnapshot('42', makeClubProfile({ club: { fan_count: 6 } }));
  assert.deepEqual(
    readHistory('42').map((h) => h.fan_count),
    [5, 6],
  );
});

test('seeds a new circle log from club_daily_history, oldest first', () => {
  const data = makeClubProfile({ club: { fan_count: 300 } });
  data.club_daily_history = [
    { actual_date: 3, rank: 10, rank_gain: 1, interpolated_fan_count: 300, interpolated_fan_gain: 100 },
    { actual_date: 1, rank: 12, rank_gain: 0, interpolated_fan_count: 100, interpolated_fan_gain: 100 },
    { actual_date: 2, rank: 11, rank_gain: 1, interpolated_fan_count: 200, interpolated_fan_gain: 100 },
  ];
  archiveSnapshot('42', data);

  const history = readHistory('42');
  assert.deepEqual(
    history.slice(0, 3).map((h) => h.fan_count),
    [100, 200, 300],
  );
  // seeded points are spaced one day apart, ending at "now"
  const t = history.slice(0, 3).map((h) => Date.parse(h.timestamp));
  assert.equal(t[1] - t[0], 86400000);
  assert.equal(t[2] - t[1], 86400000);
});

test('keeps a per-member log for current members only, backfilled from their history', () => {
  const data = makeClubProfile({
    members: [{ id: 1, name: 'Teio', history: [[1, 100], [2, 200]] }],
    pastMembers: [{ id: 2, name: 'Left', history: [[1, 50]] }],
  });
  archiveSnapshot('42', data);

  // 2 seeded rows + the current profile row
  assert.equal(readMemberHistory('42', '1').length, 3);
  assert.deepEqual(readMemberHistory('42', '2'), []);
});

test('readHistory returns only the last `limit` entries', () => {
  for (let i = 1; i <= 5; i++) archiveSnapshot('42', makeClubProfile({ club: { fan_count: i } }));
  assert.deepEqual(
    readHistory('42', 2).map((h) => h.fan_count),
    [4, 5],
  );
});
