const { fetchClubProfile } = require('./api/chronoApi');
const { themeFromEnv, LIGHT_THEME, DARK_THEME } = require('./discord/memberCardShared');

// Clubs selectable via the `club` option on /stats, /stats_simple, /quota and /set_goal.
// Both are in the same Discord server. Names, keys and circle IDs come from .env so no
// real club details live in the repo. A club is configured once its circle ID is set,
// and then needs a key and a name too. The key is what data/config.json stores the
// club's quota and goal under, so the bot refuses to start without one rather than
// quietly losing them. Club 1's colors use MEMBERS_COLOR_* and club 2's use
// CLUB_2_COLOR_*; anything unset falls back to the defaults given here. The theme is
// used by /stats, /stats_simple and the daily post.
const CLUB_SETTINGS = [
  { circleVar: 'CIRCLE_ID', keyVar: 'CLUB_KEY', nameVar: 'CLUB_NAME', theme: LIGHT_THEME },
  { circleVar: 'CIRCLE_ID_2', keyVar: 'CLUB_KEY_2', nameVar: 'CLUB_NAME_2', theme: themeFromEnv('CLUB_2', DARK_THEME) },
];
const COLOR_PREFIXES = ['MEMBERS_COLOR_', 'CLUB_2_COLOR_'];

const CLUBS = CLUB_SETTINGS.filter((s) => process.env[s.circleVar]).map((s) => {
  const missing = [s.keyVar, s.nameVar].filter((name) => !process.env[name]);
  if (missing.length > 0) {
    throw new Error(
      `${s.circleVar} is set but ${missing.join(' and ')} ${missing.length > 1 ? 'are' : 'is'} not. ` +
        'Add the missing setting to .env (see .env.example). ' +
        'The key must match the one this club\'s quota and goal are saved under in data/config.json.',
    );
  }
  return { key: process.env[s.keyVar], name: process.env[s.nameVar], circleId: process.env[s.circleVar], theme: s.theme };
});
if (CLUBS.length === 0) {
  throw new Error('CIRCLE_ID is not set in .env (see .env.example).');
}

// A color variable nothing reads (a typo, or one named after a club) would leave a card
// on its default colors without any sign why.
for (const name of Object.keys(process.env)) {
  if (name.includes('_COLOR_') && !COLOR_PREFIXES.some((prefix) => name.startsWith(prefix))) {
    console.warn(`${name} in .env is not used. Card colors are read from ${COLOR_PREFIXES.join('* and ')}*.`);
  }
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

// The club picked in a command's `club` option, or the first club when it's left out.
// Discord keeps offering the choices from the last `npm run deploy-commands`, so an old
// key can still arrive after .env changes. That gets an error reply and null, never
// another club, which /quota and /set_goal would otherwise overwrite.
async function clubFromOption(interaction) {
  const key = interaction.options.getString('club');
  if (key === null) return CLUBS[0];
  const club = CLUBS.find((c) => c.key === key);
  if (!club) {
    await interaction.reply({
      content: 'That club choice is out of date. Ask whoever runs the bot to run `npm run deploy-commands`.',
      ephemeral: true,
    });
    return null;
  }
  return club;
}

// Fetches every club's club_profile, for lookups that span all clubs (trainer linking).
// Returns [{ club, data }].
async function fetchAllClubs() {
  const results = await Promise.all(CLUBS.map((c) => fetchClubProfile(c.circleId, process.env.CHRONO_API_KEY)));
  return CLUBS.map((club, i) => ({ club, data: results[i] }));
}

module.exports = { CLUBS, addClubOption, clubFromOption, fetchAllClubs };
