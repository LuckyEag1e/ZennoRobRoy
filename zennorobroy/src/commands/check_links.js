const { SlashCommandBuilder } = require('discord.js');
const { getAllLinks, isAuthorized } = require('../config');
const { resolveTrainerName } = require('../memberStats');
const { fetchAllClubs } = require('../clubs');

module.exports = {
  data: new SlashCommandBuilder()
    .setName('check_links')
    .setDescription('List which Discord members have linked to which trainer'),

  async execute(interaction) {
    const isOwner = interaction.user.id === interaction.guild.ownerId;
    if (!isOwner && !isAuthorized(interaction.guildId, interaction.user.id)) {
      await interaction.reply({
        content: 'You are not authorised to view links. Ask the server owner to run /authorise.',
        ephemeral: true,
      });
      return;
    }

    await interaction.deferReply({ ephemeral: true });

    const links = getAllLinks(interaction.guildId);
    const userIds = Object.keys(links);

    if (userIds.length === 0) {
      await interaction.editReply('No members have linked their trainer yet.');
      return;
    }

    try {
      const clubs = await fetchAllClubs();
      const lines = userIds.map((userId) => {
        const friendViewerId = links[userId];
        for (const { club, data } of clubs) {
          const name = resolveTrainerName(data, friendViewerId);
          if (name) return `<@${userId}> → ${name} (${club.name})`;
        }
        return `<@${userId}> → (not in any current roster, id ${friendViewerId})`;
      });

      await interaction.editReply(lines.join('\n'));
    } catch (err) {
      await interaction.editReply(`Failed to resolve trainer names: ${err.message}`);
    }
  },
};
