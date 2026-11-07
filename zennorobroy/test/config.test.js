const { test, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const { resetDataDir } = require('./helpers');
const config = require('../src/config');

const GUILD = '111';
const OTHER_GUILD = '222';

// YYYY-MM-DD, `offset` days from today (UTC), the format /approve_inactivity stores.
function isoDay(offset) {
  return new Date(Date.now() + offset * 86400000).toISOString().slice(0, 10);
}

beforeEach(() => {
  resetDataDir();
  delete process.env.QUOTA_PER_DAY;
});

test('quota falls back to QUOTA_PER_DAY, then 0, until one is set', () => {
  assert.equal(config.getQuota(GUILD, 'club1'), 0);
  process.env.QUOTA_PER_DAY = '50000';
  assert.equal(config.getQuota(GUILD, 'club1'), 50000);

  config.setQuota(GUILD, 'club1', 80000);
  assert.equal(config.getQuota(GUILD, 'club1'), 80000);
});

test('a quota of 0 is kept as "disabled", not replaced by the env default', () => {
  process.env.QUOTA_PER_DAY = '50000';
  config.setQuota(GUILD, 'club1', 0);
  assert.equal(config.getQuota(GUILD, 'club1'), 0);
});

test('quota and goal are independent and scoped per guild and per club', () => {
  config.setQuota(GUILD, 'club1', 100);
  config.setGoal(GUILD, 'club2', 200);

  assert.equal(config.getQuota(GUILD, 'club2'), 0);
  assert.equal(config.getQuota(OTHER_GUILD, 'club1'), 0);
  assert.equal(config.getGoal(GUILD, 'club1'), 0);
  assert.equal(config.getGoal(GUILD, 'club2'), 200);
  assert.deepEqual([...config.getSavedClubKeys()].sort(), ['club1', 'club2']);
});

test('authorize is per guild and does not add duplicates', () => {
  config.authorize(GUILD, 'u1');
  config.authorize(GUILD, 'u1');
  assert.equal(config.isAuthorized(GUILD, 'u1'), true);
  assert.equal(config.isAuthorized(OTHER_GUILD, 'u1'), false);
});

test('trainer links store ids as strings and can be unlinked once', () => {
  config.linkTrainer(GUILD, 'u1', 123456);
  assert.equal(config.getLinkedTrainer(GUILD, 'u1'), '123456');
  assert.deepEqual(config.getAllLinks(GUILD), { u1: '123456' });

  assert.equal(config.unlinkTrainer(GUILD, 'u1'), true);
  assert.equal(config.unlinkTrainer(GUILD, 'u1'), false);
  assert.equal(config.getLinkedTrainer(GUILD, 'u1'), null);
});

// Honor system (see CLAUDE.md): two accounts may claim the same trainer on purpose.
test('two users can link the same trainer', () => {
  config.linkTrainer(GUILD, 'u1', 123);
  config.linkTrainer(GUILD, 'u2', 123);
  assert.deepEqual(config.getAllLinks(GUILD), { u1: '123', u2: '123' });
});

test('inactivity approval counts today and the end date', () => {
  assert.equal(config.setInactivityApproval(GUILD, 1, 'u1', isoDay(0)).creditDays, 1);
  assert.equal(config.setInactivityApproval(GUILD, 1, 'u1', isoDay(6)).creditDays, 7);
  // A past date still records at least one day
  assert.equal(config.setInactivityApproval(GUILD, 1, 'u1', isoDay(-3)).creditDays, 1);
});

test('inactivity credit applies through the end date, then drops to 0', () => {
  config.setInactivityApproval(GUILD, 1, 'u1', isoDay(2));
  assert.equal(config.getInactivityCredit(GUILD, 1), 3);
  assert.equal(config.getInactivityCredit(GUILD, '1'), 3);

  config.setInactivityApproval(GUILD, 2, 'u2', isoDay(0));
  assert.equal(config.getInactivityCredit(GUILD, 2), 1);

  config.setInactivityApproval(GUILD, 3, 'u3', isoDay(-1));
  assert.equal(config.getInactivityCredit(GUILD, 3), 0);

  assert.equal(config.getInactivityCredit(GUILD, 404), 0);
});

test('inactivity approvals can be listed by guild and cleared', () => {
  config.setInactivityApproval(GUILD, 1, 'u1', isoDay(3));
  assert.deepEqual(config.getGuildIdsWithInactivityApprovals(), [GUILD]);
  assert.deepEqual(Object.keys(config.getAllInactivityApprovals(GUILD)), ['1']);

  assert.equal(config.clearInactivityApproval(GUILD, 1), true);
  assert.equal(config.clearInactivityApproval(GUILD, 1), false);
  assert.equal(config.getInactivityApproval(GUILD, 1), null);
});
