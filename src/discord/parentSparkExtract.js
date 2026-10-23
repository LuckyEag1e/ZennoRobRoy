const { loadImage, createCanvas } = require('@napi-rs/canvas');
const { createWorker } = require('tesseract.js');

// Fixed layout constants for the in-game "Sparks" popup screen (1080x2316).
// Reverse-engineered from real screenshots — see reference/par1-3.jpg. If the
// game's UI ever changes these will need re-measuring the same way: OCR the
// full image for word-level bounding boxes (ground truth), then sample raw
// pixel color at candidate star positions rather than eyeballing coordinates.
const ROW0_Y = 748;
const ROW_H = 88;
const LABEL_CROP_H = 40; // short enough to stop before the star icons below the label
const STAR_Y_OFFSET = 55;
const STAR_X_OFFSET = 162; // from column left edge
const STAR_SPACING = 36;
const COL = { left: 95, right: 595 };
const COL_W = 435;
const NAME_CROP = { x0: 500, y0: 430, x1: 900, y1: 560 };
const MAX_ROWS = 14;

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

// For white-on-color pill labels (blue/pink/unique rows) that plain OCR can't
// read: threshold by luminance so bright text becomes black-on-white,
// regardless of the pill's own hue.
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

// Trailing 1-2 letter tokens are almost always OCR misreading the edge of a
// star icon just below/right of the label, not real text — strip them, but
// keep the handful of legitimately short tokens that appear in real spark names.
const KEEP_SHORT_TOKENS = new Set(['O', 'II', 'III', '+']);
function stripTrailingNoise(line) {
  const tokens = line.split(/\s+/).filter(Boolean);
  while (tokens.length > 1) {
    const last = tokens[tokens.length - 1];
    if (/^[A-Za-z]{1,2}$/.test(last) && !KEEP_SHORT_TOKENS.has(last.toUpperCase())) {
      tokens.pop();
    } else {
      break;
    }
  }
  return tokens.join(' ');
}

function cleanLabel(text) {
  let line = (text.split('\n')[0] || '').replace(/[^A-Za-z0-9 '!.:()\-★☆○+]/g, '').trim();
  line = line.replace(/^['.\s]+/, ''); // stray leading punctuation from OCR noise
  line = line.replace(/\bIl\b/g, 'II'); // common misread of the roman numeral II
  return stripTrailingNoise(line);
}

// Extracts one unit's full spark sheet (blue/pink/unique/white, each with a
// star level) plus its character name, from a single "Sparks" screenshot.
async function extractUnitSparks(worker, imagePathOrBuffer) {
  const img = await loadImage(imagePathOrBuffer);
  const canvas = createCanvas(img.width, img.height);
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0);
  const { data } = ctx.getImageData(0, 0, img.width, img.height);
  const W = img.width;

  const headerText = await plainOcrCrop(worker, img, NAME_CROP.x0, NAME_CROP.y0, NAME_CROP.x1, NAME_CROP.y1);
  const nameLines = headerText.split('\n').map((l) => l.trim()).filter(Boolean);
  const name = nameLines[nameLines.length - 1] || null;

  const blue = {
    label: cleanLabel(await thresholdOcrCrop(worker, img, COL.left, ROW0_Y, COL.left + COL_W, ROW0_Y + LABEL_CROP_H)),
    stars: starLevel(data, W, COL.left, ROW0_Y),
  };
  const pink = {
    label: cleanLabel(await thresholdOcrCrop(worker, img, COL.right, ROW0_Y, COL.right + COL_W, ROW0_Y + LABEL_CROP_H)),
    stars: starLevel(data, W, COL.right, ROW0_Y),
  };

  const uniqueRowTop = ROW0_Y + ROW_H;
  const unique = {
    label: cleanLabel(
      await thresholdOcrCrop(worker, img, COL.left, uniqueRowTop, COL.left + COL_W, uniqueRowTop + LABEL_CROP_H),
    ),
    stars: starLevel(data, W, COL.left, uniqueRowTop),
  };

  const white = [];

  // Right column: white sparks start at row index 1 (level with the unique row on the left).
  for (let r = 1; r < MAX_ROWS; r++) {
    const rowTop = ROW0_Y + r * ROW_H;
    if (rowTop + LABEL_CROP_H > img.height - 250) break;
    const text = cleanLabel(await plainOcrCrop(worker, img, COL.right, rowTop, COL.right + COL_W, rowTop + LABEL_CROP_H));
    if (!/[A-Za-z]/.test(text)) break;
    white.push({ label: text, stars: starLevel(data, W, COL.right, rowTop) });
  }

  // Left column: white sparks start at row index 2 (below blue + unique).
  for (let r = 2; r < MAX_ROWS; r++) {
    const rowTop = ROW0_Y + r * ROW_H;
    if (rowTop + LABEL_CROP_H > img.height - 250) break;
    const text = cleanLabel(await plainOcrCrop(worker, img, COL.left, rowTop, COL.left + COL_W, rowTop + LABEL_CROP_H));
    if (!/[A-Za-z]/.test(text)) break;
    white.push({ label: text, stars: starLevel(data, W, COL.left, rowTop) });
  }

  return { name, blue, pink, unique, white };
}

// Merges same-named sparks across multiple units (self + grandparents) into
// one entry per name, summing star totals and counting contributors.
function mergeSparks(entriesPerUnit) {
  const byName = new Map();
  for (const entries of entriesPerUnit) {
    for (const entry of entries) {
      if (!entry || !entry.label) continue;
      const key = entry.label.toLowerCase();
      const existing = byName.get(key);
      if (existing) {
        existing.totalStars += entry.stars;
        existing.contributors += 1;
      } else {
        byName.set(key, { label: entry.label, totalStars: entry.stars, contributors: 1 });
      }
    }
  }
  return [...byName.values()];
}

module.exports = { extractUnitSparks, mergeSparks };
