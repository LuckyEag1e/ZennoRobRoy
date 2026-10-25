const { SlashCommandBuilder } = require('discord.js');
const { getLinkedTrainer, unlinkTrainer, isAuthorized } = require('../config');

module.exports = {
  data: new SlashCommandBuilder()
    .setName('unlink_trainer')
    .setDescription("Remove a member's linked trainer")
    .addUserOption((opt) => opt.setName('user').setDescription('Member to unlink').setRequired(true)),

  async execute(interaction) {
    const isOwner = interaction.user.id === interaction.guild.ownerId;
    if (!isOwner && !isAuthorized(interaction.guildId, interaction.user.id)) {
      await interaction.reply({
        content: 'You are not authorised to unlink trainers. Ask the server owner to run /authorise.',
        ephemeral: true,
      });
      return;
    }

    const target = interaction.options.getUser('user', true);
    const linked = getLinkedTrainer(interaction.guildId, target.id);
    if (!linked) {
      await interaction.reply({ content: `${target} doesn't have a linked trainer.`, ephemeral: true });
      return;
    }

    unlinkTrainer(interaction.guildId, target.id);
    await interaction.reply(`Unlinked ${target}'s trainer.`);
  },
};
