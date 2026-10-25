const { PermissionsBitField } = require('discord.js');

const ROLE_NAME = 'Approved Inactivity';
const ROLE_COLOR = '#9a8c98';

async function findApprovedInactivityRole(guild) {
  await guild.roles.fetch();
  return guild.roles.cache.find((r) => r.name.toLowerCase() === ROLE_NAME.toLowerCase()) || null;
}

async function getOrCreateApprovedInactivityRole(guild) {
  const existing = await findApprovedInactivityRole(guild);
  if (existing) return existing;
  return guild.roles.create({
    name: ROLE_NAME,
    color: ROLE_COLOR,
    reason: 'Auto-created by /approve_inactivity for approved-inactivity tracking',
  });
}

// Assigns the role, throwing with a message suitable for surfacing directly
// to the admin who ran the command (missing permission, member not found, etc).
async function assignApprovedInactivityRole(guild, discordUserId) {
  if (!guild.members.me.permissions.has(PermissionsBitField.Flags.ManageRoles)) {
    throw new Error(`I don't have "Manage Roles" permission, so I couldn't apply the ${ROLE_NAME} role.`);
  }
  const role = await getOrCreateApprovedInactivityRole(guild);
  if (role.position >= guild.members.me.roles.highest.position) {
    throw new Error(`My highest role is below "${ROLE_NAME}" in the role list, so I couldn't assign it — move my role above it.`);
  }
  const member = await guild.members.fetch(discordUserId);
  await member.roles.add(role);
}

// Best-effort removal — used by the expiry sweep, where a missing role/member
// (left the server, role deleted) shouldn't block clearing the credit.
async function removeApprovedInactivityRole(guild, discordUserId) {
  try {
    const role = await findApprovedInactivityRole(guild);
    if (!role) return;
    const member = await guild.members.fetch(discordUserId);
    await member.roles.remove(role);
  } catch (err) {
    console.error(`Failed to remove ${ROLE_NAME} role from ${discordUserId} in guild ${guild.id}:`, err.message);
  }
}

module.exports = {
  ROLE_NAME,
  assignApprovedInactivityRole,
  removeApprovedInactivityRole,
};
