const { SlashCommandBuilder } = require('discord.js');
const { getLinkedTrainer, isAuthorized, setInactivityApproval, clearInactivityApproval } = require('../config');
const { assignApprovedInactivityRole, removeApprovedInactivityRole, ROLE_NAME } = require('../discord/inactivityRole');

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

module.exports = {
  data: new SlashCommandBuilder()
    .setName('approve_inactivity')
    .setDescription('Excuse a member from quota until a given date (past/today date revokes an existing approval)')
    .addUserOption((opt) => opt.setName('user').setDescription('Member to excuse').setRequired(true))
    .addStringOption((opt) =>
      opt.setName('end_date').setDescription('Excused through this date, YYYY-MM-DD').setRequired(true),
    ),

  async execute(interaction) {
    const isOwner = interaction.user.id === interaction.guild.ownerId;
    if (!isOwner && !isAuthorized(interaction.guildId, interaction.user.id)) {
      await interaction.reply({
        content: 'You are not authorised to approve inactivity. Ask the server owner to run /authorise.',
        ephemeral: true,
      });
      return;
    }

    const target = interaction.options.getUser('user', true);
    const endDate = interaction.options.getString('end_date', true);

    if (!DATE_RE.test(endDate) || Number.isNaN(new Date(endDate).getTime())) {
      await interaction.reply({ content: 'end_date must be in YYYY-MM-DD format, e.g. 2026-09-15.', ephemeral: true });
      return;
    }

    const friendViewerId = getLinkedTrainer(interaction.guildId, target.id);
    if (!friendViewerId) {
      await interaction.reply({
        content: `${target} hasn't linked their trainer yet — ask them to run /link_trainer first.`,
        ephemeral: true,
      });
      return;
    }

    const today = new Date().toISOString().slice(0, 10);

    if (endDate <= today) {
      clearInactivityApproval(interaction.guildId, friendViewerId);
      await removeApprovedInactivityRole(interaction.guild, target.id);
      await interaction.reply(`${target}'s approved-inactivity status has been revoked.`);
      return;
    }

    const record = setInactivityApproval(interaction.guildId, friendViewerId, target.id, endDate);

    let roleWarning = '';
    try {
      await assignApprovedInactivityRole(interaction.guild, target.id);
    } catch (err) {
      roleWarning = `\n⚠️ Quota credit was recorded, but the ${ROLE_NAME} role could not be applied: ${err.message}`;
    }

    await interaction.reply(
      `${target}'s quota requirement is excused through **${endDate}** (~${record.creditDays} day(s)).${roleWarning}`,
    );
  },
};
