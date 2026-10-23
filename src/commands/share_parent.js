const {
  SlashCommandBuilder,
  AttachmentBuilder,
  EmbedBuilder,
  ActionRowBuilder,
  ButtonBuilder,
  ButtonStyle,
} = require('discord.js');
const { loadImage } = require('@napi-rs/canvas');
const {
  fetchTrainerProfile,
  fetchFactorTable,
  fetchCharacterNames,
  fetchCharacterPortrait,
} = require('../api/umaMoeApi');
const { buildFactorIndex, resolveFactor, resolveCardName } = require('../discord/factorResolve');
const { buildParentCardImage } = require('../discord/parentCardImage');
const { mergeSparks } = require('../discord/parentSparkExtract');

// Fetches a card's portrait from uma.moe and decodes it, or null if unavailable.
async function loadPortrait(cardId) {
  const buffer = await fetchCharacterPortrait(cardId);
  if (!buffer) return null;
  try {
    return await loadImage(buffer);
  } catch {
    return null;
  }
}

// One legacy unit's raw factor fields, as returned by uma.moe's
// /api/v4/user/profile/{account_id} under `inheritance`, prefixed main_/left_/right_.
function unitSparks(inheritance, prefix, factorIndex) {
  const blueId = inheritance[`${prefix}_blue_factors`];
  const pinkId = inheritance[`${prefix}_pink_factors`];
  const greenId = inheritance[`${prefix}_green_factors`];
  const whiteIds = inheritance[`${prefix}_white_factors`] || [];

  return {
    blue: blueId != null ? [resolveFactor(blueId, factorIndex)] : [],
    pink: pinkId != null ? [resolveFactor(pinkId, factorIndex)] : [],
    unique: greenId != null ? [resolveFactor(greenId, factorIndex)] : [],
    white: whiteIds.map((id) => resolveFactor(id, factorIndex)),
  };
}

module.exports = {
  data: new SlashCommandBuilder()
    .setName('share_parent')
    .setDescription('Look up a trainer on uma.moe and generate a parent adoption card from their inheritance')
    .addStringOption((opt) =>
      opt.setName('uid').setDescription("Trainer's uma.moe account ID (9-12 digits)").setRequired(true),
    ),

  async execute(interaction) {
    const uid = interaction.options.getString('uid');
    await interaction.deferReply();

    const apiKey = process.env.UMA_MOE_API_KEY;
    if (!apiKey) {
      await interaction.editReply('UMA_MOE_API_KEY is not configured.');
      return;
    }

    let profile, factorTable, characterNames;
    try {
      [profile, factorTable, characterNames] = await Promise.all([
        fetchTrainerProfile(uid, apiKey),
        fetchFactorTable(),
        fetchCharacterNames(),
      ]);
    } catch (err) {
      await interaction.editReply(`Failed to fetch uma.moe data: ${err.message}`);
      return;
    }

    if (!profile) {
      await interaction.editReply(`No uma.moe trainer found for UID \`${uid}\`.`);
      return;
    }

    const { trainer, inheritance } = profile;
    if (!inheritance || inheritance.main_parent_id == null) {
      await interaction.editReply(`\`${trainer?.name ?? uid}\` has no inheritance data on uma.moe.`);
      return;
    }

    const factorIndex = buildFactorIndex(factorTable);
    const self = unitSparks(inheritance, 'main', factorIndex);
    const gp1 = unitSparks(inheritance, 'left', factorIndex);
    const gp2 = unitSparks(inheritance, 'right', factorIndex);

    const blueMerged = mergeSparks([self.blue, gp1.blue, gp2.blue]);
    const pinkMerged = mergeSparks([self.pink, gp1.pink, gp2.pink]);
    const uniqueMerged = mergeSparks([self.unique, gp1.unique, gp2.unique]);
    const whiteMerged = mergeSparks([self.white, gp1.white, gp2.white]);

    const selfName = resolveCardName(inheritance.main_parent_id, characterNames);
    const gp1Name = resolveCardName(inheritance.parent_left_id, characterNames);
    const gp2Name = resolveCardName(inheritance.parent_right_id, characterNames);

    const [selfPortrait, gp1Portrait, gp2Portrait] = await Promise.all([
      loadPortrait(inheritance.main_parent_id),
      loadPortrait(inheritance.parent_left_id),
      loadPortrait(inheritance.parent_right_id),
    ]);

    const imageBuffer = await buildParentCardImage({
      selfName,
      blueMerged,
      pinkMerged,
      uniqueMerged,
      whiteMerged,
      portraits: { self: selfPortrait, gp1: gp1Portrait, gp2: gp2Portrait },
    });
    const attachment = new AttachmentBuilder(imageBuffer, { name: 'parent_card.png' });

    const embed = new EmbedBuilder()
      .setTitle(selfName || `Trainer ${uid}`)
      .setColor(0x7c6ff2)
      .setDescription(`Legacies: **${gp1Name}** × **${gp2Name}**`)
      .setFooter({ text: `uma.moe UID: ${uid}${trainer?.name ? ` · Trainer: ${trainer.name}` : ''}` })
      .setImage('attachment://parent_card.png');

    const copyButton = new ButtonBuilder()
      .setCustomId(`copy_uid:${uid}`)
      .setLabel('Copy UID')
      .setStyle(ButtonStyle.Secondary);

    await interaction.editReply({
      embeds: [embed],
      files: [attachment],
      components: [new ActionRowBuilder().addComponents(copyButton)],
    });
  },
};
