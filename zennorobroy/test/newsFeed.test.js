const { test, beforeEach, afterEach, mock } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const { dataDir, resetDataDir } = require('./helpers');
const { checkNews, buildNewsEmbed } = require('../src/newsFeed');

const STATE_FILE = path.join(dataDir, 'news.json');

function item(id, { post_at = '2026-11-01 10:00:00', update_at = null, ...rest } = {}) {
  return { announce_id: id, title: `Notice ${id}`, message: '', announce_label: 1, post_at, update_at, ...rest };
}

// Stubs global fetch with the given news list and returns a fake Discord client
// that records what got sent.
function setup(items) {
  mock.method(globalThis, 'fetch', async () => ({ ok: true, json: async () => ({ information_list: items }) }));
  const sent = [];
  const client = { channels: { fetch: async () => ({ send: async (msg) => sent.push(msg.embeds[0].data) }) } };
  return { client, sent };
}

beforeEach(() => {
  resetDataDir();
  process.env.NEWS_CHANNEL_ID = 'news-channel';
  mock.method(console, 'log', () => {});
});

afterEach(() => mock.restoreAll());

test('first run seeds state silently instead of posting the backlog', async () => {
  const { client, sent } = setup([item(1), item(2)]);
  await checkNews(client);
  assert.equal(sent.length, 0);
  assert.deepEqual(Object.keys(JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')).seen), ['1', '2']);
});

test('posts only new items, oldest first, and records them as seen', async () => {
  fs.writeFileSync(STATE_FILE, JSON.stringify({ seen: { 1: null } }));
  const { client, sent } = setup([
    item(3, { post_at: '2026-11-03 10:00:00' }),
    item(2, { post_at: '2026-11-02 10:00:00' }),
    item(1),
  ]);

  await checkNews(client);

  assert.deepEqual(
    sent.map((e) => e.title),
    ['Notice 2', 'Notice 3'],
  );
  await checkNews(client);
  assert.equal(sent.length, 2, 'nothing reposted on the next poll');
});

test('reposts as [Updated] when update_at changes', async () => {
  fs.writeFileSync(STATE_FILE, JSON.stringify({ seen: { 1: null } }));
  const { client, sent } = setup([item(1, { update_at: '2026-11-05 09:00:00' })]);
  await checkNews(client);
  assert.deepEqual(
    sent.map((e) => e.title),
    ['[Updated] Notice 1'],
  );
});

test('does nothing when NEWS_CHANNEL_ID is unset', async () => {
  delete process.env.NEWS_CHANNEL_ID;
  const { client } = setup([item(1)]);
  await checkNews(client);
  assert.equal(globalThis.fetch.mock.callCount(), 0);
  assert.equal(fs.existsSync(STATE_FILE), false);
});

test('embed turns announcement HTML into readable text', () => {
  const embed = buildNewsEmbed(
    item(9, { message: '<h2>Gift</h2>Claim&nbsp;by <b>Nov 10</b><br>Tom &amp; Jerry &lt;3' }),
    false,
  ).data;
  assert.equal(embed.description, '**Gift**\nClaim by Nov 10\nTom & Jerry <3');
  assert.equal(embed.url, 'https://umamusume.com/news/9');
  assert.equal(embed.footer.text, 'umamusume.com · Game');
});

test('embed truncates long text to fit Discord limits', () => {
  const embed = buildNewsEmbed(item(9, { title: 'x'.repeat(300), message: 'y'.repeat(1000) }), false).data;
  assert.equal(embed.title.length, 256);
  assert.equal(embed.description.length, 600);
  assert.ok(embed.description.endsWith('…'));
});
