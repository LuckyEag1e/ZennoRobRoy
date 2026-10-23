// Polls the official umamusume.com news list and posts new (or updated)
// announcements to a Discord channel. The site is a SvelteKit SPA; this is the
// same JSON endpoint its news page calls client-side (undocumented).
const fs = require('fs');
const path = require('path');
const { EmbedBuilder } = require('discord.js');

const NEWS_API = 'https://umamusume.com/api/ajax/pr_info_index?format=json';
const NEWS_PAGE = 'https://umamusume.com/news/';
const STATE_FILE = path.join(__dirname, '..', 'data', 'news.json');
const FETCH_LIMIT = 20;
const CATEGORY_LABELS = { 1: 'Game', 3: 'Media' };

async function fetchNewsList() {
  const res = await fetch(NEWS_API, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0' },
    body: JSON.stringify({ announce_label: 0, limit: FETCH_LIMIT, offset: 0 }),
  });
  if (!res.ok) throw new Error(`umamusume.com news API returned ${res.status} ${res.statusText}`);
  const body = await res.json();
  if (!Array.isArray(body.information_list)) throw new Error('umamusume.com news API: unexpected response shape');
  return body.information_list;
}

// State is { seen: { [announce_id]: update_at|null } }. null on first run (no
// file yet) so the caller can seed silently instead of dumping the backlog.
function loadState() {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
  } catch (err) {
    if (err.code === 'ENOENT') return null;
    throw err;
  }
}

function saveState(state) {
  fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

function htmlToText(html) {
  // Raw newlines in the source are just HTML formatting; <br>/<h2> carry the real breaks.
  return html
    .replace(/\r?\n/g, '')
    .replace(/<h2[^>]*>\s*/gi, '\n**')
    .replace(/\s*<\/h2>/gi, '**\n')
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<[^>]+>/g, '')
    .replace(/&nbsp;/g, ' ')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&')
    .split('\n')
    .map((line) => line.replace(/ /g, ' ').trim())
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function truncate(text, max) {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
}

// API timestamps are "YYYY-MM-DD HH:MM:SS" in UTC (matches the "(UTC)" times
// quoted in the announcement bodies).
function parseApiTime(value) {
  return value ? new Date(`${value.replace(' ', 'T')}Z`) : null;
}

function buildNewsEmbed(item, isUpdate) {
  const embed = new EmbedBuilder()
    .setColor(isUpdate ? 0xf2c94c : 0x7c6ff2)
    .setTitle(truncate(`${isUpdate ? '[Updated] ' : ''}${item.title}`, 256))
    .setURL(`${NEWS_PAGE}${item.announce_id}`)
    .setDescription(truncate(htmlToText(item.message || ''), 600) || null)
    .setFooter({ text: `umamusume.com · ${CATEGORY_LABELS[item.announce_label] ?? 'News'}` })
    .setTimestamp(parseApiTime(isUpdate ? item.update_at : item.post_at));
  if (item.image) embed.setImage(item.image);
  return embed;
}

async function checkNews(client) {
  const channelId = process.env.NEWS_CHANNEL_ID;
  if (!channelId) return;

  try {
    const items = await fetchNewsList();
    const state = loadState();

    if (!state) {
      const seen = Object.fromEntries(items.map((i) => [i.announce_id, i.update_at]));
      saveState({ seen });
      console.log(`News feed: seeded ${items.length} existing announcements, nothing posted.`);
      return;
    }

    const toPost = [];
    for (const item of items) {
      if (!(item.announce_id in state.seen)) toPost.push({ item, isUpdate: false });
      else if (item.update_at && item.update_at !== state.seen[item.announce_id]) toPost.push({ item, isUpdate: true });
    }
    if (toPost.length === 0) return;

    // Oldest first so the channel reads chronologically.
    const timeOf = ({ item, isUpdate }) => parseApiTime(isUpdate ? item.update_at : item.post_at)?.getTime() ?? 0;
    toPost.sort((a, b) => timeOf(a) - timeOf(b));

    const channel = await client.channels.fetch(channelId);
    for (const entry of toPost) {
      await channel.send({ embeds: [buildNewsEmbed(entry.item, entry.isUpdate)] });
      // Mark as seen per-post so a mid-batch failure doesn't repost earlier ones.
      state.seen[entry.item.announce_id] = entry.item.update_at;
      saveState(state);
    }
    console.log(`News feed: posted ${toPost.length} announcement(s).`);
  } catch (err) {
    console.error('News feed check failed:', err);
  }
}

module.exports = { checkNews, fetchNewsList, buildNewsEmbed };
