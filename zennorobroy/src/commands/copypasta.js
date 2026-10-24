const { SlashCommandBuilder, escapeMarkdown } = require('discord.js');

const STRING_FIELDS = ['name', 'rank', 'blue_spark', 'pink_spark', 'total_blue', 'total_pink', 'white_spark_1', 'white_spark_2'];

function buildCopypasta(opts) {
  return (
    `I'm ${opts.name} with a rating of ${opts.rating}pts and ${opts.rank} rank, ` +
    `open to adopting my fellow horse babies into a warm, financially and mentally stable household. ` +
    `My personal income includes ${opts.blue_spark} sparks and ${opts.pink_spark} sparks, ` +
    `while my total family inheritance sits at a generous ${opts.total_blue} sparks and ${opts.total_pink} sparks ` +
    `passed down through generations of hardworking Umas. ` +
    `The estate also comes fully staffed with ${opts.white_spark_1} and ${opts.white_spark_2}. ` +
    `Every scar on these legs is a love letter to the finish line, and I promise to pass down not just my sparks, ` +
    `but the fire in my heart because a true family isn't measured in stats and rating, it's measured in how many times ` +
    `you got back up after losing to the most rando horse S+ rating MANT made in your last CM. Bloodline is temporary, but glory is forever.`
  );
}

module.exports = {
  data: new SlashCommandBuilder()
    .setName('copypasta')
    .setDescription('Generate the parent adoption copypasta')
    .addStringOption((opt) => opt.setName('name').setDescription('Your uma\'s name').setRequired(true))
    .addIntegerOption((opt) => opt.setName('rating').setDescription('Your rating (pts)').setRequired(true))
    .addStringOption((opt) => opt.setName('rank').setDescription('Your rank (e.g. SS, S+, A)').setRequired(true))
    .addStringOption((opt) => opt.setName('blue_spark').setDescription('Blue spark (e.g. "3★ Speed")').setRequired(true))
    .addStringOption((opt) => opt.setName('pink_spark').setDescription('Pink spark (e.g. "3★ Turf")').setRequired(true))
    .addStringOption((opt) => opt.setName('total_blue').setDescription('Total blue spark').setRequired(true))
    .addStringOption((opt) => opt.setName('total_pink').setDescription('Total pink spark').setRequired(true))
    .addStringOption((opt) => opt.setName('white_spark_1').setDescription('White spark 1').setRequired(true))
    .addStringOption((opt) => opt.setName('white_spark_2').setDescription('White spark 2').setRequired(true)),

  async execute(interaction) {
    const opts = { rating: interaction.options.getInteger('rating') };
    for (const field of STRING_FIELDS) {
      opts[field] = escapeMarkdown(interaction.options.getString(field));
    }

    await interaction.reply(buildCopypasta(opts));
  },
};
