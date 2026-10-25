const { SlashCommandBuilder } = require('discord.js');
const { linkTrainer } = require('../config');
const { fetchAllClubs } = require('../clubs');

module.exports = {
  data: new SlashCommandBuilder()
    .setName('link_trainer')
    .setDescription('Link your Discord account to your in-game trainer name (needed for /approve_inactivity)')
    .addStringOption((opt) => opt.setName('name').setDescription('Your exact in-game trainer name').setRequired(true)),

  async execute(interaction) {
    const name = interaction.options.getString('name', true);
    await interaction.deferReply({ ephemeral: true });

    try {
      // Searches every club's current roster, so members of any club can link.
      const matches = [];
      for (const { club, data } of await fetchAllClubs()) {
        const currentIds = new Set(data.club?.[0]?.circle_user_array || []);
        for (const p of data.club_friend_profile || []) {
          if (currentIds.has(p.friend_viewer_id) && p.name.toLowerCase() === name.toLowerCase()) {
            matches.push({ ...p, clubName: club.name });
          }
        }
      }

      if (matches.length === 0) {
        await interaction.editReply(`No current club member found named "${name}" (check spelling — case-insensitive).`);
        return;
      }
      if (matches.length > 1) {
        await interaction.editReply(`Multiple current members are named "${name}" — this can't be resolved automatically.`);
        return;
      }

      linkTrainer(interaction.guildId, interaction.user.id, matches[0].friend_viewer_id);
      await interaction.editReply(`Linked your Discord account to trainer "${matches[0].name}" (${matches[0].clubName}).`);
    } catch (err) {
      await interaction.editReply(`Failed to look up trainer: ${err.message}`);
    }
  },
};
