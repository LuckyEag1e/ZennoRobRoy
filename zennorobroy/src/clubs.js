const { fetchClubProfile } = require('./api/chronoApi');
const { themeFromEnv, LIGHT_THEME, DARK_THEME } = require('./discord/memberCardShared');

// Clubs selectable via the `club` option on /stats and /stats_simple. Both are in
// the same Discord server. Names, keys and circle IDs come from .env so no real
// club details live in the repo. The key is what data/config.json stores per-club
// settings under, so changing it orphans that club's quota and goal. Club 1's
// colors use the MEMBERS_COLOR_* names and club 2's use CLUB_2_COLOR_*; anything
// unset falls back to the defaults given here. The theme is used by /stats,
// /stats_simple and the daily post.
const CLUBS = [
  {
    key: process.env.CLUB_KEY || 'club_1',
    name: process.env.CLUB_NAME || 'Club 1',
    circleId: process.env.CIRCLE_ID,
    theme: LIGHT_THEME,
  },
  {
    key: process.env.CLUB_KEY_2 || 'club_2',
    name: process.env.CLUB_NAME_2 || 'Club 2',
    circleId: process.env.CIRCLE_ID_2,
    theme: themeFromEnv('CLUB_2', DARK_THEME),
  },
];

function getClub(key) {
  return CLUBS.find((c) => c.key === key) || CLUBS[0];
}

function addClubOption(builder) {
  return builder.addStringOption((opt) =>
    opt
      .setName('club')
      .setDescription(`Which club to show (default: ${CLUBS[0].name})`)
      .setRequired(false)
      .addChoices(...CLUBS.map((c) => ({ name: c.name, value: c.key }))),
  );
}

// Fetches every configured club's club_profile, for lookups that span all clubs
// (trainer linking). Returns [{ club, data }].
async function fetchAllClubs() {
  const configured = CLUBS.filter((c) => c.circleId);
  const results = await Promise.all(configured.map((c) => fetchClubProfile(c.circleId, process.env.CHRONO_API_KEY)));
  return configured.map((club, i) => ({ club, data: results[i] }));
}

module.exports = { CLUBS, getClub, addClubOption, fetchAllClubs };
