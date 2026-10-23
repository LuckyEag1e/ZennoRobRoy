const fs = require('fs');
const path = require('path');

const DATA_DIR = path.join(__dirname, '..', 'data');

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

// IDs (circle_id, friend_viewer_id) are always numeric game IDs. Reject anything
// else before it touches a filesystem path — path.join doesn't strip "../"
// segments, so an unvalidated ID would otherwise let a caller write/read files
// anywhere on disk the bot process can reach.
function assertValidId(id, label) {
  if (!/^[0-9]+$/.test(String(id))) {
    throw new Error(`Invalid ${label}: ${id}`);
  }
}

function readLastLine(file) {
  if (!fs.existsSync(file)) return null;
  const content = fs.readFileSync(file, 'utf8').trim();
  if (!content) return null;
  const lines = content.split('\n');
  return JSON.parse(lines[lines.length - 1]);
}

// The Chrono Genesis API only ticks over once per real day, so re-fetching within
// that window (e.g. running several commands back to back) returns identical
// numbers. Appending every fetch unconditionally packed the history logs with
// runs of duplicate points — harmless for the raw values, but since the trend
// charts space points evenly by index rather than by real time, a burst of
// identical re-fetches visually reads as the trend flattening out/plateauing
// when nothing actually changed. Skip the append when nothing but the
// timestamp differs from the last recorded line.
function appendIfChanged(file, record, compareKeys) {
  const last = readLastLine(file);
  if (last && compareKeys.every((k) => last[k] === record[k])) return;
  fs.appendFileSync(file, JSON.stringify(record) + '\n');
}

// Saves the full raw API response (the archive) and appends a lightweight
// summary line to a per-circle history log for building long-term trends, plus
// a per-member history log for /profile (see archiveMemberSnapshots below).
function archiveSnapshot(circleId, data) {
  assertValidId(circleId, 'circle_id');
  const timestamp = new Date().toISOString();

  const rawDir = path.join(DATA_DIR, 'raw', String(circleId));
  ensureDir(rawDir);
  const safeStamp = timestamp.replace(/[:.]/g, '-');
  fs.writeFileSync(path.join(rawDir, `${safeStamp}.json`), JSON.stringify(data, null, 2));

  const club = data.club && data.club[0];
  if (club) {
    const historyDir = path.join(DATA_DIR, 'history');
    ensureDir(historyDir);
    const historyFile = path.join(historyDir, `${circleId}.jsonl`);

    // First time we see this circle: seed the local log with the API's
    // existing daily history so the trend chart isn't empty on day one.
    if (!fs.existsSync(historyFile) && Array.isArray(data.club_daily_history) && data.club_daily_history.length > 0) {
      const seedLines = seedFromDailyHistory(data.club_daily_history, timestamp);
      fs.writeFileSync(historyFile, seedLines.map((l) => JSON.stringify(l)).join('\n') + '\n');
    }

    const record = {
      timestamp,
      rank: club.rank,
      rank_diff: club.rank_diff,
      fan_count: club.fan_count,
      daily_diff: club.daily_diff,
      monthly_average: club.monthly_average,
      member_num: club.member_num,
    };
    appendIfChanged(historyFile, record, ['rank', 'rank_diff', 'fan_count', 'daily_diff', 'monthly_average', 'member_num']);
  }

  archiveMemberSnapshots(circleId, data, timestamp);

  return timestamp;
}

function seedFromDailyHistory(dailyHistory, nowIso) {
  const now = new Date(nowIso).getTime();
  const sorted = [...dailyHistory].sort((a, b) => a.actual_date - b.actual_date);
  const maxDate = sorted[sorted.length - 1].actual_date;

  return sorted.map((d) => ({
    timestamp: new Date(now - (maxDate - d.actual_date) * 86400000).toISOString(),
    rank: d.rank,
    rank_diff: d.rank_gain,
    fan_count: d.interpolated_fan_count,
    daily_diff: d.interpolated_fan_gain,
    monthly_average: null,
    member_num: null,
  }));
}

// Same backfill trick as seedFromDailyHistory, but per member: club_friend_history
// carries each member's own day-by-day interpolated_fan_count for the current
// period, so a brand-new member history file doesn't start as a single dot.
function seedMemberFromHistory(historyRows, nowIso) {
  const now = new Date(nowIso).getTime();
  const sorted = [...historyRows].sort((a, b) => a.actual_date - b.actual_date);
  const maxDate = sorted[sorted.length - 1].actual_date;

  return sorted.map((h) => ({
    timestamp: new Date(now - (maxDate - h.actual_date) * 86400000).toISOString(),
    name: h.friend_name,
    fan_count: h.interpolated_fan_count,
    daily_average: null,
    monthly_average: null,
    daily_diff: h.adjusted_interpolated_fan_gain,
    monthly_diff: null,
  }));
}

// One append-only log per current member (data/history/members/<circleId>/<friend_viewer_id>.jsonl),
// so /profile can chart a trend that survives period resets — club_friend_history
// only covers the current period, this accumulates indefinitely across fetches.
function archiveMemberSnapshots(circleId, data, timestamp) {
  const club = data.club && data.club[0];
  if (!club) return;

  const currentIds = new Set(club.circle_user_array || []);
  if (currentIds.size === 0) return;

  const membersDir = path.join(DATA_DIR, 'history', 'members', String(circleId));
  ensureDir(membersDir);

  const historyByMember = new Map();
  for (const h of data.club_friend_history || []) {
    if (!currentIds.has(h.friend_viewer_id)) continue;
    if (!historyByMember.has(h.friend_viewer_id)) historyByMember.set(h.friend_viewer_id, []);
    historyByMember.get(h.friend_viewer_id).push(h);
  }

  for (const profile of data.club_friend_profile || []) {
    if (!currentIds.has(profile.friend_viewer_id)) continue;
    assertValidId(profile.friend_viewer_id, 'friend_viewer_id');

    const file = path.join(membersDir, `${profile.friend_viewer_id}.jsonl`);

    if (!fs.existsSync(file)) {
      const rows = historyByMember.get(profile.friend_viewer_id) || [];
      if (rows.length > 0) {
        const seedLines = seedMemberFromHistory(rows, timestamp);
        fs.writeFileSync(file, seedLines.map((l) => JSON.stringify(l)).join('\n') + '\n');
      }
    }

    const record = {
      timestamp,
      name: profile.name,
      fan_count: profile.fan_count,
      daily_average: profile.daily_average,
      monthly_average: profile.monthly_average,
      daily_diff: profile.daily_diff,
      monthly_diff: profile.monthly_diff,
    };
    appendIfChanged(file, record, ['name', 'fan_count', 'daily_average', 'monthly_average', 'daily_diff', 'monthly_diff']);
  }
}

function readHistory(circleId, limit = 90) {
  assertValidId(circleId, 'circle_id');
  const file = path.join(DATA_DIR, 'history', `${circleId}.jsonl`);
  if (!fs.existsSync(file)) return [];
  const lines = fs.readFileSync(file, 'utf8').trim().split('\n').filter(Boolean);
  return lines.slice(-limit).map((line) => JSON.parse(line));
}

function readMemberHistory(circleId, friendViewerId, limit = 90) {
  assertValidId(circleId, 'circle_id');
  assertValidId(friendViewerId, 'friend_viewer_id');
  const file = path.join(DATA_DIR, 'history', 'members', String(circleId), `${friendViewerId}.jsonl`);
  if (!fs.existsSync(file)) return [];
  const lines = fs.readFileSync(file, 'utf8').trim().split('\n').filter(Boolean);
  return lines.slice(-limit).map((line) => JSON.parse(line));
}

module.exports = { archiveSnapshot, readHistory, readMemberHistory };
