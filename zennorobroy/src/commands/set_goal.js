const { SlashCommandBuilder } = require('discord.js');
const { getGoal, setGoal, isAuthorized } = require('../config');
const { addClubOption, clubFromOption } = require('../clubs');

module.exports = {
  data: addClubOption(
    new SlashCommandBuilder()
      .setName('set_goal')
      .setDescription('View or set the aspirational daily fan gain goal per member (0 disables the goal line)')
      .addIntegerOption((opt) =>
        opt.setName('amount').setDescription('New goal per day (0 to disable)').setRequired(false).setMinValue(0),
      ),
  ),

  async execute(interaction) {
    const amount = interaction.options.getInteger('amount');
    const club = await clubFromOption(interaction);
    if (!club) return;

    if (amount === null) {
      const current = getGoal(interaction.guildId, club.key);
      await interaction.reply({
        content: current > 0 ? `${club.name} goal: ${current.toLocaleString()}/day` : `Goal tracking is currently disabled for ${club.name}.`,
        ephemeral: true,
      });
      return;
    }

    const isOwner = interaction.user.id === interaction.guild.ownerId;
    if (!isOwner && !isAuthorized(interaction.guildId, interaction.user.id)) {
      await interaction.reply({
        content: 'You are not authorised to change the goal. Ask the server owner to run /authorise.',
        ephemeral: true,
      });
      return;
    }

    setGoal(interaction.guildId, club.key, amount);
    await interaction.reply(amount === 0 ? `Goal tracking disabled for ${club.name}.` : `${club.name} goal set to ${amount.toLocaleString()}/day.`);
  },
};
