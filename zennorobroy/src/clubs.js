const { fetchClubProfile } = require('./api/chronoApi');
const { themeFromEnv, LIGHT_THEME, DARK_THEME } = require('./discord/memberCardShared');

// Clubs selectable via the `club` option on /stats and /stats_simple. Both are in
// the same Discord server. Each club's colors come from <colorPrefix>_COLOR_* in
// .env (TOKAI TEIO keeps the original MEMBERS_COLOR_* names); anything unset falls
// back to the defaults given here. The theme is used by /stats, /stats_simple and
// the daily post.
const CLUBS = [
  {
    key: 'tokai_teio',
    name: 'TOKAI TEIO',
    circleId: process.env.CIRCLE_ID,
    theme: LIGHT_THEME,
  },
  {
    key: 'hachimiers',
    name: 'Hachimiers',
    circleId: process.env.CIRCLE_ID_2,
    theme: themeFromEnv('HACHIMIERS', DARK_THEME),
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
