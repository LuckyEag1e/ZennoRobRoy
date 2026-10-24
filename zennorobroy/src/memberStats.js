// Builds the per-member leaderboard from the raw club_profile response.
// club_friend_profile/club_friend_history include past members too, so we
// filter to circle_user_array (the current roster) to match member_num.
function computeMemberStats(data) {
  const club = data.club && data.club[0];
  if (!club) return { members: [], day: 0, periodGain: 0 };

  const currentIds = new Set(club.circle_user_array || []);
  const profileById = new Map(
    (data.club_friend_profile || []).filter((p) => currentIds.has(p.friend_viewer_id)).map((p) => [p.friend_viewer_id, p]),
  );

  const historyById = new Map();
  for (const h of data.club_friend_history || []) {
    if (!currentIds.has(h.friend_viewer_id)) continue;
    if (!historyById.has(h.friend_viewer_id)) historyById.set(h.friend_viewer_id, []);
    historyById.get(h.friend_viewer_id).push(h);
  }

  let day = 0;
  const members = [];

  for (const id of currentIds) {
    const profile = profileById.get(id);
    const history = (historyById.get(id) || []).sort((a, b) => a.actual_date - b.actual_date);
    const name = profile ? profile.name : history.length > 0 ? history[history.length - 1].friend_name : `Trainer ${id}`;

    if (history.length === 0) {
      members.push({ id, name, avg: 0, gain: 0, latestGain: 0, days: 0 });
      continue;
    }

    const last = history[history.length - 1];
    day = Math.max(day, last.actual_date);
    const days = history.length;
    const gain = last.adjusted_fan_gain_cumulative;

    members.push({
      id,
      name,
      avg: days > 0 ? gain / days : 0,
      gain,
      latestGain: last.adjusted_interpolated_fan_gain,
      days,
    });
  }

  members.sort((a, b) => b.avg - a.avg);

  const periodGain = members.reduce((sum, m) => sum + m.gain, 0);

  return { members, day, periodGain };
}

// Resolves a friend_viewer_id to their current in-game name, or null if
// they're not on the current roster (e.g. left the club since linking).
function resolveTrainerName(data, friendViewerId) {
  const club = data.club && data.club[0];
  if (!club) return null;

  const currentIds = new Set(club.circle_user_array || []);
  const key = String(friendViewerId);
  const profile = (data.club_friend_profile || []).find(
    (p) => currentIds.has(p.friend_viewer_id) && String(p.friend_viewer_id) === key,
  );
  return profile ? profile.name : null;
}

module.exports = { computeMemberStats, resolveTrainerName };
