const { createWorker } = require('tesseract.js');
const fs = require('fs');
const { extractUnitSparks, mergeSparks } = require('./src/discord/parentSparkExtract.js');
const { buildParentCardImage } = require('./src/discord/parentCardImage.js');

(async () => {
  const worker = await createWorker('eng');
  const self = await extractUnitSparks(worker, './reference/par1.jpg');
  const gp1 = await extractUnitSparks(worker, './reference/par2.jpg');
  const gp2 = await extractUnitSparks(worker, './reference/par3.jpg');
  await worker.terminate();

  const blueMerged = mergeSparks([[self.blue], [gp1.blue], [gp2.blue]]);
  const pinkMerged = mergeSparks([[self.pink], [gp1.pink], [gp2.pink]]);
  const uniqueMerged = mergeSparks([[self.unique], [gp1.unique], [gp2.unique]]);
  const whiteMerged = mergeSparks([self.white, gp1.white, gp2.white]);

  // Stand-in for player-picked highlights (would come from a Discord select menu) —
  // deliberately picking 5 here (not 2-3) to prove the count is flexible.
  const highlightedLabels = [...whiteMerged].sort((a, b) => b.totalStars - a.totalStars).slice(0, 5).map((s) => s.label);

  const buffer = await buildParentCardImage({
    selfName: self.name,
    blueMerged,
    pinkMerged,
    uniqueMerged,
    whiteMerged,
    highlightedLabels,
    portraits: {},
  });

  fs.writeFileSync('./scratch_parent_card.png', buffer);
  console.log('wrote scratch_parent_card.png');
  console.log('highlighted:', highlightedLabels);
})();
