const fs = require('fs');
const path = require('path');
const { createCanvas, loadImage } = require('@napi-rs/canvas');
const { COLOR_BG, COLOR_SECTION, COLOR_BORDER, COLOR_TEXT, COLOR_MUTED, COLOR_YELLOW, roundRect } = require('./memberCardShared');

const WIDTH = 900;
const PAD = 24;
const PORTRAIT_BOX_W = 210;
const PORTRAIT_BOX_H = 230;

const PILL_H = 34;
const PILL_H_HIGHLIGHT = 40;
const PILL_GAP_X = 10;
const PILL_GAP_Y = 10;
const PILL_FONT = '15px sans-serif';
const PILL_FONT_HIGHLIGHT = 'bold 17px sans-serif';

const CAT_COLORS = {
  // Blue/pink/unique pills: dark fill (not a solid color block) — the
  // category shows through as the border + text color instead.
  blue: { bg: COLOR_SECTION, text: '#5fb4f0', border: '#5fb4f0', borderWidth: 2 },
  pink: { bg: COLOR_SECTION, text: '#e078b8', border: '#e078b8', borderWidth: 2 },
  unique: { bg: COLOR_SECTION, text: '#a3d94a', border: '#a3d94a', borderWidth: 2 },
  white: { bg: '#2a2740', text: COLOR_TEXT },
  // Highlighted white sparks: same family as the plain white pill, just a
  // touch warmer, with a thin, low-contrast border rather than a loud outline.
  highlight: { bg: '#332e22', text: COLOR_YELLOW, border: '#8a7a4a' },
};

const PORTRAITS_DIR = path.join(__dirname, '..', '..', 'assets', 'uma_portraits');

function normalizeName(name) {
  return (name || '').toLowerCase().trim().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
}

async function findPortrait(name) {
  const base = normalizeName(name);
  if (!base) return null;
  for (const ext of ['.png', '.jpg', '.jpeg', '.webp']) {
    const p = path.join(PORTRAITS_DIR, base + ext);
    if (fs.existsSync(p)) {
      try {
        return await loadImage(p);
      } catch {
        return null;
      }
    }
  }
  return null;
}

function drawCircularPortrait(ctx, img, cx, cy, r, fallbackLabel) {
  ctx.save();
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.closePath();
  ctx.clip();
  if (img) {
    const size = r * 2;
    ctx.drawImage(img, cx - r, cy - r, size, size);
  } else {
    ctx.fillStyle = COLOR_SECTION;
    ctx.fillRect(cx - r, cy - r, r * 2, r * 2);
    ctx.fillStyle = COLOR_MUTED;
    ctx.font = `bold ${Math.round(r)}px sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText((fallbackLabel || '?').slice(0, 1).toUpperCase(), cx, cy);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }
  ctx.restore();
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.strokeStyle = COLOR_BORDER;
  ctx.lineWidth = 3;
  ctx.stroke();
}

function pillWidth(ctx, text, font) {
  ctx.font = font;
  return ctx.measureText(text).width + 28;
}

// Sparks that appear on more than one unit are already summed into a single
// total (see mergeSparks) — the pill just shows that one final number, no
// separate "×N contributors" notation (which read as a multiplier, not a count).
function pillText(entry) {
  return `${entry.label} ${entry.totalStars ?? entry.stars}★`;
}

// Draws a row of pills, wrapping to new lines as needed. Highlighted entries
// (in highlightedKeys) render larger with a subtle border and are expected to
// already be sorted first in `entries` by the caller. Line height adapts to
// the tallest pill actually placed on that line. Returns the y just below the row band.
function drawPillRow(ctx, x0, xMax, y, entries, category, highlightedKeys) {
  let x = x0;
  let curY = y;
  let lineH = 0;

  for (const entry of entries) {
    const key = entry.label.toLowerCase();
    const isHighlighted = highlightedKeys && highlightedKeys.has(key);
    const colors = isHighlighted ? CAT_COLORS.highlight : CAT_COLORS[category];
    const font = isHighlighted ? PILL_FONT_HIGHLIGHT : PILL_FONT;
    const h = isHighlighted ? PILL_H_HIGHLIGHT : PILL_H;

    const text = pillText(entry);
    const w = pillWidth(ctx, text, font);

    if (x + w > xMax) {
      x = x0;
      curY += lineH + PILL_GAP_Y;
      lineH = 0;
    }

    roundRect(ctx, x, curY, w, h, h / 2);
    ctx.fillStyle = colors.bg;
    ctx.fill();
    if (colors.border) {
      ctx.strokeStyle = colors.border;
      ctx.lineWidth = colors.borderWidth ?? 1;
      ctx.stroke();
    }

    ctx.fillStyle = colors.text;
    ctx.font = font;
    ctx.textBaseline = 'middle';
    ctx.fillText(text, x + 14, curY + h / 2 + 1);
    ctx.textBaseline = 'alphabetic';

    x += w + PILL_GAP_X;
    lineH = Math.max(lineH, h);
  }
  return curY + lineH;
}

// opts: { selfName, blueMerged, pinkMerged, uniqueMerged, whiteMerged, highlightedLabels, portraits: { self, gp1, gp2 } }
// blue/pink/unique/white *Merged arrays are each the output of mergeSparks()
// scoped to that category — same-named sparks across self+GP1+GP2 combined
// into one pill with a summed star total and contributor count.
async function buildParentCardImage(opts) {
  const { selfName, blueMerged, pinkMerged, uniqueMerged, whiteMerged, highlightedLabels = [], portraits = {} } = opts;
  const highlightedKeys = new Set(highlightedLabels.map((l) => l.toLowerCase()));

  // Highlighted sparks render first in the list, in whatever order they were
  // picked; everything else keeps its original (star-total descending) order.
  const orderedWhite = [
    ...whiteMerged.filter((e) => highlightedKeys.has(e.label.toLowerCase())),
    ...whiteMerged.filter((e) => !highlightedKeys.has(e.label.toLowerCase())),
  ];

  // Pre-measure height by doing a dry layout pass on an offscreen canvas.
  const measureCanvas = createCanvas(WIDTH, 4000);
  const mctx = measureCanvas.getContext('2d');

  const contentX0 = PAD + PORTRAIT_BOX_W + PAD;
  const contentXMax = WIDTH - PAD;

  let y = PAD + 40;
  y = drawPillRow(mctx, contentX0, contentXMax, y, blueMerged, 'blue') + PILL_GAP_Y;
  y = drawPillRow(mctx, contentX0, contentXMax, y, pinkMerged, 'pink') + PILL_GAP_Y;
  y = drawPillRow(mctx, contentX0, contentXMax, y, uniqueMerged, 'unique') + PILL_GAP_Y + 10;

  const portraitBottomY = PAD + PORTRAIT_BOX_H;
  const afterHeaderY = Math.max(y, portraitBottomY) + 20;

  let listY = afterHeaderY + 30; // room for "White Sparks" label
  listY = drawPillRow(mctx, PAD, WIDTH - PAD, listY, orderedWhite, 'white', highlightedKeys);

  const totalH = listY + PAD;

  // Real render pass.
  const canvas = createCanvas(WIDTH, totalH);
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = COLOR_BG;
  ctx.fillRect(0, 0, WIDTH, totalH);

  // Portrait cluster box.
  roundRect(ctx, PAD, PAD, PORTRAIT_BOX_W, PORTRAIT_BOX_H, 14);
  ctx.fillStyle = COLOR_SECTION;
  ctx.fill();
  ctx.strokeStyle = COLOR_BORDER;
  ctx.lineWidth = 2;
  ctx.stroke();

  const selfR = 62;
  const gpR = 38;
  const selfCx = PAD + 20 + selfR;
  const selfCy = PAD + PORTRAIT_BOX_H / 2;
  drawCircularPortrait(ctx, portraits.self, selfCx, selfCy, selfR, selfName);

  const gpCx = PAD + PORTRAIT_BOX_W - 20 - gpR;
  const gp1Cy = PAD + 55;
  const gp2Cy = PAD + PORTRAIT_BOX_H - 55;
  drawCircularPortrait(ctx, portraits.gp1, gpCx, gp1Cy, gpR, 'GP1');
  drawCircularPortrait(ctx, portraits.gp2, gpCx, gp2Cy, gpR, 'GP2');

  ctx.fillStyle = COLOR_TEXT;
  ctx.font = 'bold 24px sans-serif';
  ctx.fillText(selfName || 'Unknown', contentX0, PAD + 28);

  y = PAD + 40;
  y = drawPillRow(ctx, contentX0, contentXMax, y, blueMerged, 'blue') + PILL_GAP_Y;
  y = drawPillRow(ctx, contentX0, contentXMax, y, pinkMerged, 'pink') + PILL_GAP_Y;
  y = drawPillRow(ctx, contentX0, contentXMax, y, uniqueMerged, 'unique') + PILL_GAP_Y + 10;

  ctx.fillStyle = COLOR_MUTED;
  ctx.font = '14px sans-serif';
  ctx.fillText(`White Sparks: ${whiteMerged.length}`, PAD, afterHeaderY + 12);

  listY = afterHeaderY + 30;
  listY = drawPillRow(ctx, PAD, WIDTH - PAD, listY, orderedWhite, 'white', highlightedKeys);

  return canvas.toBuffer('image/png');
}

module.exports = { buildParentCardImage, findPortrait, normalizeName };
