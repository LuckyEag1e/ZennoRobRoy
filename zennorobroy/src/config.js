const fs = require('fs');
const path = require('path');

const CONFIG_FILE = path.join(__dirname, '..', 'data', 'config.json');

function load() {
  if (!fs.existsSync(CONFIG_FILE)) return { quota: {}, authorized: {} };
  return JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
}

function save(config) {
  fs.mkdirSync(path.dirname(CONFIG_FILE), { recursive: true });
  fs.writeFileSync(CONFIG_FILE, JSON.stringify(config, null, 2));
}

// Quota and goal are per club within a guild (clubKey from src/clubs.js), since
// more than one club can share a Discord server.
// 0 (or unset with no env default) means quota tracking is disabled.
function getQuota(guildId, clubKey) {
  const config = load();
  const value = config.clubQuota?.[guildId]?.[clubKey];
  return value !== undefined ? value : Number(process.env.QUOTA_PER_DAY ?? 0);
}

function setQuota(guildId, clubKey, amount) {
  const config = load();
  config.clubQuota = config.clubQuota || {};
  config.clubQuota[guildId] = config.clubQuota[guildId] || {};
  config.clubQuota[guildId][clubKey] = amount;
  save(config);
}

// Separate from quota: an aspirational stretch target rather than a hard requirement.
// 0 means the goal line is disabled. No .env fallback — unlike quota this has no
// pre-existing default, so an unset goal is just off until someone runs /set_goal.
function getGoal(guildId, clubKey) {
  const config = load();
  return config.clubGoal?.[guildId]?.[clubKey] ?? 0;
}

function setGoal(guildId, clubKey, amount) {
  const config = load();
  config.clubGoal = config.clubGoal || {};
  config.clubGoal[guildId] = config.clubGoal[guildId] || {};
  config.clubGoal[guildId][clubKey] = amount;
  save(config);
}

// Every club key that has a saved quota or goal in any guild.
function getSavedClubKeys() {
  const config = load();
  const keys = new Set();
  for (const byGuild of [config.clubQuota, config.clubGoal]) {
    for (const byClub of Object.values(byGuild || {})) {
      Object.keys(byClub).forEach((key) => keys.add(key));
    }
  }
  return keys;
}

function isAuthorized(guildId, userId) {
  const config = load();
  return (config.authorized?.[guildId] || []).includes(userId);
}

function authorize(guildId, userId) {
  const config = load();
  config.authorized = config.authorized || {};
  config.authorized[guildId] = config.authorized[guildId] || [];
  if (!config.authorized[guildId].includes(userId)) {
    config.authorized[guildId].push(userId);
  }
  save(config);
}

// Links a Discord user to their in-game trainer (friend_viewer_id), so commands
// like /approve_inactivity can resolve a @mention to a club_profile row.
function getLinkedTrainer(guildId, userId) {
  const config = load();
  return config.trainerLinks?.[guildId]?.[userId] ?? null;
}

function linkTrainer(guildId, userId, friendViewerId) {
  const config = load();
  config.trainerLinks = config.trainerLinks || {};
  config.trainerLinks[guildId] = config.trainerLinks[guildId] || {};
  config.trainerLinks[guildId][userId] = String(friendViewerId);
  save(config);
}

// All Discord user -> friend_viewer_id links in a guild, for /check_links.
function getAllLinks(guildId) {
  const config = load();
  return config.trainerLinks?.[guildId] || {};
}

function unlinkTrainer(guildId, userId) {
  const config = load();
  if (!config.trainerLinks?.[guildId]?.[userId]) return false;
  delete config.trainerLinks[guildId][userId];
  save(config);
  return true;
}

// Approved-inactivity, per trainer: { discordUserId, endDate, creditDays }.
// creditDays is the length of the excused window in days, computed once at
// approval time from today -> endDate (see setInactivityApproval), not
// recomputed daily — matches the old flat-credit behavior, just derived from
// a date instead of typed in by hand. Cleared automatically once endDate has
// passed (see index.js's daily expiry sweep), or immediately if
// /approve_inactivity is re-run with a past/today end date.
function getInactivityApproval(guildId, friendViewerId) {
  const config = load();
  return config.inactivityApprovals?.[guildId]?.[String(friendViewerId)] ?? null;
}

// All approvals in a guild, keyed by friendViewerId — for the daily expiry sweep.
function getAllInactivityApprovals(guildId) {
  const config = load();
  return config.inactivityApprovals?.[guildId] || {};
}

// Every guild that has at least one approval on record, for the daily expiry
// sweep to know which guilds to check without iterating every guild the bot is in.
function getGuildIdsWithInactivityApprovals() {
  const config = load();
  return Object.keys(config.inactivityApprovals || {});
}

function setInactivityApproval(guildId, friendViewerId, discordUserId, endDate) {
  const config = load();
  config.inactivityApprovals = config.inactivityApprovals || {};
  config.inactivityApprovals[guildId] = config.inactivityApprovals[guildId] || {};

  const today = new Date(new Date().toISOString().slice(0, 10));
  const end = new Date(endDate);
  const creditDays = Math.max(1, Math.round((end - today) / 86400000));

  const record = { discordUserId, endDate, creditDays };
  config.inactivityApprovals[guildId][String(friendViewerId)] = record;
  save(config);
  return record;
}

function clearInactivityApproval(guildId, friendViewerId) {
  const config = load();
  if (!config.inactivityApprovals?.[guildId]?.[String(friendViewerId)]) return false;
  delete config.inactivityApprovals[guildId][String(friendViewerId)];
  save(config);
  return true;
}

// Number of excused days to apply to a member's quota calculation. Returns 0
// once the approval's endDate has passed, even if the daily sweep hasn't
// gotten around to clearing it and removing the role yet.
function getInactivityCredit(guildId, friendViewerId) {
  const approval = getInactivityApproval(guildId, friendViewerId);
  if (!approval) return 0;
  const today = new Date().toISOString().slice(0, 10);
  if (approval.endDate <= today) return 0;
  return approval.creditDays;
}

module.exports = {
  getQuota,
  setQuota,
  getGoal,
  setGoal,
  getSavedClubKeys,
  isAuthorized,
  authorize,
  getLinkedTrainer,
  linkTrainer,
  getAllLinks,
  unlinkTrainer,
  getInactivityApproval,
  getAllInactivityApprovals,
  getGuildIdsWithInactivityApprovals,
  setInactivityApproval,
  clearInactivityApproval,
  getInactivityCredit,
};
