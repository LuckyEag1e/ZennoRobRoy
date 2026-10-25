const { SlashCommandBuilder, PermissionFlagsBits } = require('discord.js');
const { authorize } = require('../config');

module.exports = {
  data: new SlashCommandBuilder()
    .setName('authorise')
    .setDescription('Grant a member permission to change the club quota (server owner only)')
    .addUserOption((opt) => opt.setName('user').setDescription('User to authorise').setRequired(true))
    .setDefaultMemberPermissions(PermissionFlagsBits.Administrator),

  async execute(interaction) {
    if (interaction.user.id !== interaction.guild.ownerId) {
      await interaction.reply({ content: 'Only the server owner can use this command.', ephemeral: true });
      return;
    }

    const target = interaction.options.getUser('user', true);
    authorize(interaction.guildId, target.id);
    await interaction.reply({ content: `${target} is now authorised to change the club quota.`, ephemeral: true });
  },
};
