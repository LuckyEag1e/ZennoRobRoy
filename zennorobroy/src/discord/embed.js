const { EmbedBuilder } = require('discord.js');

const COLOR_IMPROVED = 0x57f287;
const COLOR_WORSE = 0xed4245;
const COLOR_NEUTRAL = 0x5865f2;

// Thin wrapper embed around the locally-rendered report image (stats and
// chart live in the PNG itself, so this just frames it).
function buildClubEmbed(data, circleId) {
  const club = data.club && data.club[0];
  if (!club) {
    throw new Error(`No club data found for circle_id ${circleId}`);
  }

  const rankDiff = club.rank_diff ?? 0;
  const color = rankDiff > 0 ? COLOR_IMPROVED : rankDiff < 0 ? COLOR_WORSE : COLOR_NEUTRAL;

  const embed = new EmbedBuilder()
    .setTitle(`${club.name} — Club Report`)
    .setColor(color)
    .setFooter({ text: `Circle ID: ${circleId}` })
    .setTimestamp(club.updated_at ? new Date(club.updated_at) : new Date())
    .setImage('attachment://report.png');

  if (club.comment) {
    embed.setDescription(club.comment);
  }

  return embed;
}

module.exports = { buildClubEmbed };
