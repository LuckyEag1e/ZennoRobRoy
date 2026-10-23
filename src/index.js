require('dotenv').config();
const fs = require('fs');
const path = require('path');
const cron = require('node-cron');
const { Client, GatewayIntentBits, Collection, AttachmentBuilder } = require('discord.js');
const { fetchClubProfile } = require('./api/chronoApi');
const { buildMemberDetailedImage } = require('./discord/memberDetailedImage');
const { archiveSnapshot } = require('./archive');
const {
  getQuota,
  getGoal,
  getGuildIdsWithInactivityApprovals,
  getAllInactivityApprovals,
  clearInactivityApproval,
} = require('./config');
const { removeApprovedInactivityRole } = require('./discord/inactivityRole');
const { checkNews } = require('./newsFeed');
const { CLUBS } = require('./clubs');

const client = new Client({ intents: [GatewayIntentBits.Guilds] });
client.commands = new Collection();

const commandsPath = path.join(__dirname, 'commands');
for (const file of fs.readdirSync(commandsPath).filter((f) => f.endsWith('.js'))) {
  const command = require(path.join(commandsPath, file));
  client.commands.set(command.data.name, command);
}

client.once('ready', () => {
  console.log(`Logged in as ${client.user.tag}`);
  scheduleDailyPost();
  scheduleInactivityExpirySweep();
  scheduleNewsFeed();
});

// Best-effort DM to the admin before the process exits. Only covers deaths the
// process gets to see coming (graceful shutdown, uncaught JS errors) — a
// `kill -9`, OOM kill, or host crash/reboot gives no chance to run this.
async function notifyAdmin(message) {
  const adminId = process.env.ADMIN_USER_ID;
  if (!adminId) return;
  try {
    const user = await client.users.fetch(adminId);
    await user.send(message);
  } catch (err) {
    console.error('Failed to DM admin shutdown notice:', err);
  }
}

async function shutdown(signal) {
  console.log(`Received ${signal}, shutting down.`);
  await notifyAdmin(`⚠️ Uma bot is shutting down (${signal}).`);
  process.exit(0);
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

process.on('uncaughtException', async (err) => {
  console.error('Uncaught exception:', err);
  await notifyAdmin(`🔴 Uma bot crashed (uncaught exception): \`${err.message}\``);
  process.exit(1);
});

process.on('unhandledRejection', async (reason) => {
  console.error('Unhandled rejection:', reason);
  await notifyAdmin(`🔴 Uma bot crashed (unhandled rejection): \`${reason}\``);
  process.exit(1);
});

client.on('interactionCreate', async (interaction) => {
  if (interaction.isButton() && interaction.customId.startsWith('copy_uid:')) {
    const uid = interaction.customId.slice('copy_uid:'.length);
    await interaction.reply({ content: `\`${uid}\``, ephemeral: true });
    return;
  }

  if (!interaction.isChatInputCommand()) return;

  const command = client.commands.get(interaction.commandName);
  if (!command) return;

  try {
    await command.execute(interaction);
  } catch (err) {
    console.error(`Error running /${interaction.commandName}:`, err);
    const reply = { content: 'Something went wrong running that command.', ephemeral: true };
    if (interaction.deferred || interaction.replied) {
      await interaction.editReply(reply);
    } else {
      await interaction.reply(reply);
    }
  }
});

function scheduleDailyPost() {
  const hour = Number(process.env.POST_HOUR_UTC ?? 9);
  const cronExpr = `0 ${hour} * * *`;

  cron.schedule(cronExpr, () => postDailySummary(), { timezone: 'UTC' });
  console.log(`Daily club summary scheduled for ${hour}:00 UTC.`);
}

// Posts one /stats card per configured club to CHANNEL_ID. Each club is
// handled separately so one failed fetch doesn't block the others.
async function postDailySummary() {
  let channel;
  try {
    channel = await client.channels.fetch(process.env.CHANNEL_ID);
  } catch (err) {
    console.error('Failed to post daily summary:', err);
    return;
  }

  for (const club of CLUBS.filter((c) => c.circleId)) {
    try {
      const data = await fetchClubProfile(club.circleId, process.env.CHRONO_API_KEY);
      archiveSnapshot(club.circleId, data);

      const quotaPerDay = getQuota(channel.guildId, club.key);
      const goalPerDay = getGoal(channel.guildId, club.key);
      const imageBuffer = buildMemberDetailedImage(data, club.circleId, quotaPerDay, goalPerDay, channel.guildId, club.theme);
      const attachment = new AttachmentBuilder(imageBuffer, { name: 'stats.png' });

      await channel.send({ files: [attachment] });
      console.log(`Posted daily summary for ${club.name}.`);
    } catch (err) {
      console.error(`Failed to post daily summary for ${club.name}:`, err);
    }
  }
}

// Independent of scheduleDailyPost (which the user toggles on/off) — approved-
// inactivity roles should expire on schedule regardless of whether the daily
// club-summary post is enabled.
function scheduleInactivityExpirySweep() {
  cron.schedule('5 0 * * *', () => checkInactivityExpiries(), { timezone: 'UTC' });
  console.log('Approved-inactivity expiry sweep scheduled for 00:05 UTC.');
}

async function checkInactivityExpiries() {
  const today = new Date().toISOString().slice(0, 10);

  for (const guildId of getGuildIdsWithInactivityApprovals()) {
    const approvals = getAllInactivityApprovals(guildId);
    const expired = Object.entries(approvals).filter(([, record]) => record.endDate <= today);
    if (expired.length === 0) continue;

    try {
      const guild = await client.guilds.fetch(guildId);
      for (const [friendViewerId, record] of expired) {
        await removeApprovedInactivityRole(guild, record.discordUserId);
        clearInactivityApproval(guildId, friendViewerId);
        console.log(`Approved-inactivity expired for ${record.discordUserId} in guild ${guildId}.`);
      }
    } catch (err) {
      console.error(`Failed to sweep inactivity approvals for guild ${guildId}:`, err);
    }
  }
}

// Polls umamusume.com for new announcements (banners, gift distributions,
// events). Disabled unless NEWS_CHANNEL_ID is set.
function scheduleNewsFeed() {
  if (!process.env.NEWS_CHANNEL_ID) {
    console.log('News feed disabled (NEWS_CHANNEL_ID not set).');
    return;
  }
  cron.schedule('*/10 * * * *', () => checkNews(client), { timezone: 'UTC' });
  checkNews(client);
  console.log('News feed polling umamusume.com every 10 minutes.');
}

client.login(process.env.DISCORD_TOKEN);
