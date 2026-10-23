const { SlashCommandBuilder, AttachmentBuilder } = require('discord.js');
const { fetchClubProfile } = require('../api/chronoApi');
const { buildMemberImage } = require('../discord/memberImage');
const { archiveSnapshot } = require('../archive');
const { getQuota, getGoal } = require('../config');
const { getClub, addClubOption } = require('../clubs');

module.exports = {
  data: addClubOption(
    new SlashCommandBuilder()
      .setName('stats_simple')
      .setDescription('Show per-member performance for the club'),
  ),

  async execute(interaction) {
    const club = getClub(interaction.options.getString('club'));
    const circleId = club.circleId;
    if (!circleId) {
      await interaction.reply({ content: `No circle ID is configured for ${club.name}.`, ephemeral: true });
      return;
    }
    await interaction.deferReply();

    try {
      const data = await fetchClubProfile(circleId, process.env.CHRONO_API_KEY);
      archiveSnapshot(circleId, data);

      const quotaPerDay = getQuota(interaction.guildId, club.key);
      const goalPerDay = getGoal(interaction.guildId, club.key);
      const imageBuffer = buildMemberImage(data, circleId, quotaPerDay, goalPerDay, interaction.guildId, club.theme);
      const attachment = new AttachmentBuilder(imageBuffer, { name: 'stats_simple.png' });

      await interaction.editReply({ files: [attachment] });
    } catch (err) {
      await interaction.editReply(`Failed to fetch member data: ${err.message}`);
    }
  },
};
