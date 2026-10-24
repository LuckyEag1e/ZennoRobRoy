const { loadImage, createCanvas } = require('@napi-rs/canvas');
const { createWorker } = require('tesseract.js');

const ROW0_Y = 748;
const ROW_H = 88;
const LABEL_H = 68; // crop height for label OCR within a row
const STAR_Y_OFFSET = 55;
const STAR_SPACING = 36;
const STAR_X_OFFSET = 162; // from column left edge
const COL = { left: 95, right: 595 };
const COL_W = 435;

function px(data, W, x, y) {
  const i = (Math.round(y) * W + Math.round(x)) * 4;
  return [data[i], data[i + 1], data[i + 2]];
}

function starLevel(data, W, colX0, rowTop) {
  let level = 0;
  for (let i = 0; i < 3; i++) {
    const x = colX0 + STAR_X_OFFSET + i * STAR_SPACING;
    const y = rowTop + STAR_Y_OFFSET;
    const [r, g, b] = px(data, W, x, y);
    if (r - b > 70 && g > 140) level++;
  }
  return level;
}

async function plainOcrCrop(worker, img, x0, y0, x1, y1) {
  const w = x1 - x0, h = y1 - y0;
  const canvas = createCanvas(w * 3, h * 3);
  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(img, x0, y0, w, h, 0, 0, w * 3, h * 3);
  const { data: res } = await worker.recognize(canvas.toBuffer('image/png'));
  return res.text.trim();
}

async function thresholdOcrCrop(worker, img, x0, y0, x1, y1) {
  const w = x1 - x0, h = y1 - y0;
  const scale = 4;
  const canvas = createCanvas(w * scale, h * scale);
  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(img, x0, y0, w, h, 0, 0, w * scale, h * scale);
  const imgData = ctx.getImageData(0, 0, w * scale, h * scale);
  const d = imgData.data;
  for (let i = 0; i < d.length; i += 4) {
    const lum = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
    const v = lum > 190 ? 0 : 255;
    d[i] = d[i + 1] = d[i + 2] = v;
  }
  ctx.putImageData(imgData, 0, 0);
  const { data: res } = await worker.recognize(canvas.toBuffer('image/png'));
  return res.text.trim();
}

function firstLine(text) {
  return (text.split('\n')[0] || '').replace(/[^A-Za-z0-9 '!.:()\-★☆○+]/g, '').trim();
}

async function extract(worker, path) {
  const img = await loadImage(path);
  const canvas = createCanvas(img.width, img.height);
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0);
  const { data } = ctx.getImageData(0, 0, img.width, img.height);
  const W = img.width;

  // Name: OCR the header region broadly, take the longer/lower line.
  const headerText = await plainOcrCrop(worker, img, 500, 430, 900, 560);
  const nameLines = headerText.split('\n').map((l) => l.trim()).filter(Boolean);
  const name = nameLines[nameLines.length - 1] || '(unreadable)';

  const blueLabel = firstLine(await thresholdOcrCrop(worker, img, COL.left, ROW0_Y, COL.left + COL_W, ROW0_Y + LABEL_H));
  const pinkLabel = firstLine(await thresholdOcrCrop(worker, img, COL.right, ROW0_Y, COL.right + COL_W, ROW0_Y + LABEL_H));
  const blueStars = starLevel(data, W, COL.left, ROW0_Y);
  const pinkStars = starLevel(data, W, COL.right, ROW0_Y);

  const uniqueRowTop = ROW0_Y + ROW_H;
  const uniqueLabel = firstLine(await thresholdOcrCrop(worker, img, COL.left, uniqueRowTop, COL.left + COL_W, uniqueRowTop + LABEL_H));
  const uniqueStars = starLevel(data, W, COL.left, uniqueRowTop);

  const whiteSparks = [];

  // Right column: white sparks start at row index 1 (same row as unique on the left).
  for (let r = 1; r < 14; r++) {
    const rowTop = ROW0_Y + r * ROW_H;
    if (rowTop + LABEL_H > img.height - 250) break;
    const text = firstLine(await plainOcrCrop(worker, img, COL.right, rowTop, COL.right + COL_W, rowTop + LABEL_H));
    if (!/[A-Za-z]/.test(text)) break;
    whiteSparks.push({ label: text, stars: starLevel(data, W, COL.right, rowTop), col: 'right' });
  }

  // Left column: white sparks start at row index 2 (below blue + unique).
  for (let r = 2; r < 14; r++) {
    const rowTop = ROW0_Y + r * ROW_H;
    if (rowTop + LABEL_H > img.height - 250) break;
    const text = firstLine(await plainOcrCrop(worker, img, COL.left, rowTop, COL.left + COL_W, rowTop + LABEL_H));
    if (!/[A-Za-z]/.test(text)) break;
    whiteSparks.push({ label: text, stars: starLevel(data, W, COL.left, rowTop), col: 'left' });
  }

  return { name, blue: { blueLabel, blueStars }, pink: { pinkLabel, pinkStars }, unique: { uniqueLabel, uniqueStars }, whiteSparks };
}

(async () => {
  const worker = await createWorker('eng');
  for (const path of ['./reference/par1.jpg', './reference/par2.jpg', './reference/par3.jpg']) {
    console.log(`\n========== ${path} ==========`);
    const result = await extract(worker, path);
    console.log('name:', result.name);
    console.log('blue:', result.blue.blueLabel, result.blue.blueStars + '★');
    console.log('pink:', result.pink.pinkLabel, result.pink.pinkStars + '★');
    console.log('unique:', result.unique.uniqueLabel, result.unique.uniqueStars + '★');
    console.log('white sparks:');
    for (const w of result.whiteSparks) console.log(`  [${w.col}] ${w.label} - ${w.stars}★`);
  }
  await worker.terminate();
})();
