const { SlashCommandBuilder, AttachmentBuilder } = require('discord.js');
const { fetchClubProfile } = require('../api/chronoApi');
const { buildMemberDetailedImage } = require('../discord/memberDetailedImage');
const { archiveSnapshot } = require('../archive');
const { getQuota, getGoal } = require('../config');
const { addClubOption, clubFromOption } = require('../clubs');

module.exports = {
  data: addClubOption(
    new SlashCommandBuilder()
      .setName('stats')
      .setDescription('Show the full per-member breakdown (Avg/Gain/+latest/Days)'),
  ),

  async execute(interaction) {
    const club = await clubFromOption(interaction);
    if (!club) return;
    const circleId = club.circleId;
    await interaction.deferReply();

    try {
      const data = await fetchClubProfile(circleId, process.env.CHRONO_API_KEY);
      archiveSnapshot(circleId, data);

      const quotaPerDay = getQuota(interaction.guildId, club.key);
      const goalPerDay = getGoal(interaction.guildId, club.key);
      const imageBuffer = buildMemberDetailedImage(data, circleId, quotaPerDay, goalPerDay, interaction.guildId, club.theme);
      const attachment = new AttachmentBuilder(imageBuffer, { name: 'stats.png' });

      await interaction.editReply({ files: [attachment] });
    } catch (err) {
      await interaction.editReply(`Failed to fetch member data: ${err.message}`);
    }
  },
};
