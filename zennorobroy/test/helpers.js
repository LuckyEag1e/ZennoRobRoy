// Shared test setup. Require this BEFORE any src/ module: config.js, archive.js and
// newsFeed.js read ZENNO_DATA_DIR once at load time, and without it they would write
// to the live data/ directory. `node --test` runs each test file in its own process,
// so every file gets a fresh temp dir.
const fs = require('fs');
const os = require('os');
const path = require('path');

const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'zenno-test-'));
process.env.ZENNO_DATA_DIR = dataDir;
process.on('exit', () => fs.rmSync(dataDir, { recursive: true, force: true }));

// Empties the temp data dir between tests so state doesn't leak.
function resetDataDir() {
  for (const entry of fs.readdirSync(dataDir)) {
    fs.rmSync(path.join(dataDir, entry), { recursive: true, force: true });
  }
}

// A minimal club_profile response, shaped like the real Chrono Genesis API.
// `members` is [{ id, name, history: [[actual_date, cumulative, latestGain], ...] }].
function makeClubProfile({ members = [], pastMembers = [], club = {} } = {}) {
  const all = [...members, ...pastMembers];
  return {
    club: [
      {
        rank: 100,
        rank_diff: 0,
        fan_count: 1_000_000,
        daily_diff: 0,
        monthly_average: 0,
        member_num: members.length,
        circle_user_array: members.map((m) => m.id),
        ...club,
      },
    ],
    club_daily_history: [],
    club_friend_profile: all.map((m) => ({
      friend_viewer_id: m.id,
      name: m.name,
      fan_count: 0,
      daily_average: 0,
      monthly_average: 0,
      daily_diff: 0,
      monthly_diff: 0,
    })),
    club_friend_history: all.flatMap((m) =>
      (m.history || []).map(([actual_date, cumulative, latestGain = 0]) => ({
        friend_viewer_id: m.id,
        friend_name: m.name,
        actual_date,
        adjusted_fan_gain_cumulative: cumulative,
        adjusted_interpolated_fan_gain: latestGain,
        interpolated_fan_count: cumulative,
      })),
    ),
  };
}

module.exports = { dataDir, resetDataDir, makeClubProfile };
