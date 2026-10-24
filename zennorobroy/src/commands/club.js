const { SlashCommandBuilder, AttachmentBuilder } = require('discord.js');
const { fetchClubProfile } = require('../api/chronoApi');
const { buildClubEmbed } = require('../discord/embed');
const { buildReportImage } = require('../discord/reportImage');
const { archiveSnapshot, readHistory } = require('../archive');

module.exports = {
  data: new SlashCommandBuilder()
    .setName('club')
    .setDescription('Look up the latest Chrono Genesis club report'),

  async execute(interaction) {
    const circleId = process.env.CIRCLE_ID;
    await interaction.deferReply();

    try {
      const data = await fetchClubProfile(circleId, process.env.CHRONO_API_KEY);
      archiveSnapshot(circleId, data);
      const history = readHistory(circleId);

      const embed = buildClubEmbed(data, circleId);
      const imageBuffer = buildReportImage(data, circleId, history);
      const attachment = new AttachmentBuilder(imageBuffer, { name: 'report.png' });

      await interaction.editReply({ embeds: [embed], files: [attachment] });
    } catch (err) {
      await interaction.editReply(`Failed to fetch club data: ${err.message}`);
    }
  },
};
