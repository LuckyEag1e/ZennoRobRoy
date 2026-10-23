const { SlashCommandBuilder } = require('discord.js');
const { getQuota, setQuota, isAuthorized } = require('../config');
const { getClub, addClubOption } = require('../clubs');

module.exports = {
  data: addClubOption(
    new SlashCommandBuilder()
      .setName('quota')
      .setDescription('View or set the required daily fan gain per member (0 disables quota tracking)')
      .addIntegerOption((opt) =>
        opt.setName('amount').setDescription('New quota per day (0 to disable)').setRequired(false).setMinValue(0),
      ),
  ),

  async execute(interaction) {
    const amount = interaction.options.getInteger('amount');
    const club = getClub(interaction.options.getString('club'));

    if (amount === null) {
      const current = getQuota(interaction.guildId, club.key);
      await interaction.reply({
        content: current > 0 ? `${club.name} quota: ${current.toLocaleString()}/day` : `Quota tracking is currently disabled for ${club.name}.`,
        ephemeral: true,
      });
      return;
    }

    const isOwner = interaction.user.id === interaction.guild.ownerId;
    if (!isOwner && !isAuthorized(interaction.guildId, interaction.user.id)) {
      await interaction.reply({
        content: 'You are not authorised to change the quota. Ask the server owner to run /authorise.',
        ephemeral: true,
      });
      return;
    }

    setQuota(interaction.guildId, club.key, amount);
    await interaction.reply(amount === 0 ? `Quota tracking disabled for ${club.name}.` : `${club.name} quota set to ${amount.toLocaleString()}/day.`);
  },
};
