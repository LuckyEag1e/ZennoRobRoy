require('dotenv').config();
const fs = require('fs');
const path = require('path');
const { REST, Routes } = require('discord.js');

const commands = [];
const commandsPath = path.join(__dirname, 'commands');
for (const file of fs.readdirSync(commandsPath).filter((f) => f.endsWith('.js'))) {
  const command = require(path.join(commandsPath, file));
  commands.push(command.data.toJSON());
}

const rest = new REST().setToken(process.env.DISCORD_TOKEN);

// GUILD_ID may be a single ID or a comma-separated list, for registering
// instantly to more than one dev/test server.
const guildIds = (process.env.GUILD_ID || '')
  .split(',')
  .map((id) => id.trim())
  .filter(Boolean);

(async () => {
  try {
    if (guildIds.length === 0) {
      const data = await rest.put(Routes.applicationCommands(process.env.CLIENT_ID), { body: commands });
      console.log(`Registered ${data.length} slash command(s) globally.`);
      return;
    }

    for (const guildId of guildIds) {
      const data = await rest.put(Routes.applicationGuildCommands(process.env.CLIENT_ID, guildId), {
        body: commands,
      });
      console.log(`Registered ${data.length} slash command(s) guild ${guildId}.`);
    }
  } catch (err) {
    console.error('Failed to register commands:', err);
    process.exitCode = 1;
  }
})();
