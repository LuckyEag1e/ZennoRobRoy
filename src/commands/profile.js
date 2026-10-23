const { SlashCommandBuilder, AttachmentBuilder } = require('discord.js');
const { fetchClubProfile } = require('../api/chronoApi');
const { archiveSnapshot, readMemberHistory } = require('../archive');
const { getLinkedTrainer } = require('../config');
const { buildProfileImage } = require('../discord/profileImage');

module.exports = {
  data: new SlashCommandBuilder()
    .setName('profile')
    .setDescription("Look up a member's profile and fan-count trend over time")
    .addUserOption((opt) => opt.setName('user').setDescription('Discord user (defaults to you)').setRequired(false))
    .addStringOption((opt) =>
      opt.setName('name').setDescription('Exact in-game trainer name (overrides user)').setRequired(false),
    ),

  async execute(interaction) {
    const circleId = process.env.CIRCLE_ID;
    await interaction.deferReply();

    try {
      const data = await fetchClubProfile(circleId, process.env.CHRONO_API_KEY);
      archiveSnapshot(circleId, data);

      const club = data.club && data.club[0];
      if (!club) {
        await interaction.editReply(`No club data found for circle_id ${circleId}`);
        return;
      }
      const currentIds = new Set(club.circle_user_array || []);

      const nameOpt = interaction.options.getString('name');
      const userOpt = interaction.options.getUser('user');

      let friendViewerId;

      if (nameOpt) {
        const matches = (data.club_friend_profile || []).filter(
          (p) => currentIds.has(p.friend_viewer_id) && p.name.toLowerCase() === nameOpt.toLowerCase(),
        );
        if (matches.length === 0) {
          await interaction.editReply(`No current club member found named "${nameOpt}" (check spelling — case-insensitive).`);
          return;
        }
        if (matches.length > 1) {
          await interaction.editReply(`Multiple current members are named "${nameOpt}" — this can't be resolved automatically.`);
          return;
        }
        friendViewerId = matches[0].friend_viewer_id;
      } else {
        const targetUser = userOpt || interaction.user;
        friendViewerId = getLinkedTrainer(interaction.guildId, targetUser.id);
        if (!friendViewerId) {
          await interaction.editReply(
            targetUser.id === interaction.user.id
              ? "You haven't linked a trainer yet — run /link_trainer first, or pass a name directly."
              : `<@${targetUser.id}> hasn't linked a trainer yet.`,
          );
          return;
        }
      }

      const profile = (data.club_friend_profile || []).find(
        (p) => currentIds.has(p.friend_viewer_id) && String(p.friend_viewer_id) === String(friendViewerId),
      );
      if (!profile) {
        await interaction.editReply("That trainer is linked but no longer in the club's current roster.");
        return;
      }

      const history = readMemberHistory(circleId, friendViewerId);
      const imageBuffer = buildProfileImage(profile, circleId, history);
      const attachment = new AttachmentBuilder(imageBuffer, { name: 'profile.png' });

      await interaction.editReply({ files: [attachment] });
    } catch (err) {
      await interaction.editReply(`Failed to fetch profile: ${err.message}`);
    }
  },
};
