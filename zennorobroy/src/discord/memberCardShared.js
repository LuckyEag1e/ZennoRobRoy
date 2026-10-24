const COLOR_BG = '#12111f';
const COLOR_SECTION = '#1e1b34';
const COLOR_BORDER = '#7c6ff2';
const COLOR_TEXT = '#f2f0ff';
const COLOR_MUTED = '#9b96c2';
const COLOR_GREEN = '#57f287';
const COLOR_RED = '#ff5c8a';
const COLOR_YELLOW = '#f2c94c';

// Default (dark purple) palette, used by /profile and as the fallback for club themes. Shared
// drawing helpers below take an optional theme so a card can swap palettes.
const DARK_THEME = {
  bg: COLOR_BG,
  section: COLOR_SECTION,
  border: COLOR_BORDER,
  text: COLOR_TEXT,
  muted: COLOR_MUTED,
  green: COLOR_GREEN,
  red: COLOR_RED,
  yellow: COLOR_YELLOW,
};

// Light pink/crimson palette for TOKAI TEIO on /stats_simple. Each color can be overridden from .env
// (MEMBERS_COLOR_*); anything unset or not a valid #rrggbb hex falls back to the
// default here. Status colors are darker than the dark theme's so they stay
// readable on the pale background.
function envColor(name, fallback) {
  const value = (process.env[name] || '').trim();
  if (!value) return fallback;
  if (/^#[0-9a-fA-F]{6}$/.test(value)) return value;
  console.warn(`${name}="${value}" is not a #rrggbb hex color, using ${fallback}`);
  return fallback;
}

// Builds a palette from <PREFIX>_COLOR_* in .env, falling back per-color to
// `defaults`. Used for per-club color schemes (see src/clubs.js).
function themeFromEnv(prefix, defaults) {
  return {
    bg: envColor(`${prefix}_COLOR_BG`, defaults.bg),
    section: envColor(`${prefix}_COLOR_SECTION`, defaults.section),
    border: envColor(`${prefix}_COLOR_BORDER`, defaults.border),
    text: envColor(`${prefix}_COLOR_TEXT`, defaults.text),
    muted: envColor(`${prefix}_COLOR_SUBTEXT`, defaults.muted),
    green: envColor(`${prefix}_COLOR_GREEN`, defaults.green),
    red: envColor(`${prefix}_COLOR_RED`, defaults.red),
    yellow: envColor(`${prefix}_COLOR_YELLOW`, defaults.yellow),
  };
}

const LIGHT_THEME = themeFromEnv('MEMBERS', {
  bg: '#f1d7d6',
  section: '#f1d7d6',
  border: '#950001',
  text: '#000000',
  muted: '#950001',
  green: '#1e7b34',
  red: '#e0002a',
  yellow: '#b7791f',
});

const HEADER_H = 110;
const STATS_H = 160;

function compact(n) {
  const sign = n < 0 ? '-' : '';
  const abs = Math.abs(n);
  if (abs >= 1e6) return `${sign}${(abs / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${sign}${Math.round(abs / 1e3)}K`;
  return `${sign}${Math.round(abs)}`;
}

function formatFull(n) {
  return new Intl.NumberFormat('en-US').format(Math.round(n));
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function section(ctx, x, y, w, h, theme = DARK_THEME) {
  roundRect(ctx, x, y, w, h, 12);
  ctx.fillStyle = theme.section;
  ctx.fill();
  ctx.strokeStyle = theme.border;
  ctx.lineWidth = 2;
  ctx.stroke();
}

function rankChangeText(diff, theme = DARK_THEME) {
  if (diff > 0) return { text: `▲ ${diff}`, color: theme.green };
  if (diff < 0) return { text: `▼ ${Math.abs(diff)}`, color: theme.red };
  return { text: '– no change', color: theme.muted };
}

function truncate(ctx, text, maxWidth) {
  if (ctx.measureText(text).width <= maxWidth) return text;
  let out = text;
  while (out.length > 1 && ctx.measureText(`${out}…`).width > maxWidth) {
    out = out.slice(0, -1);
  }
  return `${out}…`;
}

function drawHeader(ctx, x, y, w, club, circleId, day, theme = DARK_THEME) {
  section(ctx, x, y, w, HEADER_H, theme);
  ctx.fillStyle = theme.text;
  ctx.font = 'bold 26px sans-serif';
  ctx.fillText(club.name, x + 20, y + 40);

  ctx.fillStyle = theme.muted;
  ctx.font = '16px sans-serif';
  ctx.fillText(`${circleId} · Day ${day}`, x + 20, y + 68);

  const rankDiff = club.rank_diff ?? 0;
  const rankChange = rankChangeText(rankDiff, theme);
  ctx.font = 'bold 24px sans-serif';
  ctx.fillStyle = theme.text;
  const rankLabel = `#${club.rank}`;
  ctx.fillText(rankLabel, x + w - 20 - ctx.measureText(rankLabel).width, y + 40);
  ctx.font = '16px sans-serif';
  ctx.fillStyle = rankChange.color;
  ctx.fillText(rankChange.text, x + w - 20 - ctx.measureText(rankChange.text).width, y + 68);
}

// { club, quotaEnabled, periodGain, buffer, quotaPerDay, expectedGain, day,
//   goalEnabled, goalPerDay, goalExpectedGain, theme }
function drawStatsBox(ctx, x, y, w, opts) {
  const {
    club,
    quotaEnabled,
    periodGain,
    buffer,
    quotaPerDay,
    expectedGain,
    day,
    goalEnabled,
    goalPerDay,
    goalExpectedGain,
    theme = DARK_THEME,
  } = opts;
  section(ctx, x, y, w, STATS_H, theme);

  const statCol1 = x + 20;
  const statCol2 = x + w / 2 + 10;

  ctx.fillStyle = theme.muted;
  ctx.font = '14px sans-serif';
  ctx.fillText('Fans', statCol1, y + 24);
  ctx.fillText('Buffer', statCol2, y + 24);

  ctx.fillStyle = theme.text;
  ctx.font = 'bold 18px sans-serif';
  ctx.fillText(`${formatFull(club.fan_count)} (+${compact(periodGain)})`, statCol1, y + 46);
  ctx.fillStyle = buffer === null ? theme.muted : buffer >= 0 ? theme.green : theme.red;
  ctx.fillText(buffer === null ? '–' : `${buffer >= 0 ? '+' : ''}${compact(buffer)}`, statCol2, y + 46);

  ctx.fillStyle = theme.muted;
  ctx.font = '14px sans-serif';
  ctx.fillText('Quota', statCol1, y + 76);
  ctx.fillText('Expected Gain', statCol2, y + 76);

  ctx.fillStyle = theme.text;
  ctx.font = 'bold 16px sans-serif';
  ctx.fillText(quotaEnabled ? `${compact(quotaPerDay)}/day` : 'Disabled', statCol1, y + 96);
  ctx.fillText(quotaEnabled ? `${compact(expectedGain)} (Day ${day})` : '–', statCol2, y + 96);

  ctx.fillStyle = theme.muted;
  ctx.font = '14px sans-serif';
  ctx.fillText('Goal', statCol1, y + 128);
  ctx.fillText('Goal Gain', statCol2, y + 128);

  ctx.fillStyle = theme.yellow;
  ctx.font = 'bold 16px sans-serif';
  ctx.fillText(goalEnabled ? `${compact(goalPerDay)}/day` : 'Disabled', statCol1, y + 148);
  ctx.fillText(goalEnabled ? `${compact(goalExpectedGain)} (Day ${day})` : '–', statCol2, y + 148);
}

// Divider line drawn between the table row where a threshold (quota or goal) is met
// and where it isn't. Quota is the hard requirement, drawn bold/solid/red. Goal is
// an aspirational stretch target, drawn subtle/dashed/yellow so it doesn't compete
// visually with quota — optionally with a centered label (e.g. "Goal") breaking up
// the dashes, so it doesn't get mistaken for the quota line at a glance.
function drawThresholdLine(ctx, xStart, xEnd, y, { color, subtle = false, label } = {}) {
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = subtle ? 1 : 2;
  ctx.globalAlpha = subtle ? 0.55 : 1;
  if (subtle) ctx.setLineDash([4, 3]);

  if (!label) {
    ctx.beginPath();
    ctx.moveTo(xStart, y);
    ctx.lineTo(xEnd, y);
    ctx.stroke();
    ctx.restore();
    return;
  }

  ctx.font = 'bold 11px sans-serif';
  const textWidth = ctx.measureText(label).width;
  const midX = (xStart + xEnd) / 2;
  const textStart = midX - textWidth / 2;
  const gap = 8;

  ctx.beginPath();
  ctx.moveTo(xStart, y);
  ctx.lineTo(textStart - gap, y);
  ctx.moveTo(textStart + textWidth + gap, y);
  ctx.lineTo(xEnd, y);
  ctx.stroke();

  ctx.setLineDash([]);
  ctx.globalAlpha = 1;
  ctx.textAlign = 'left';
  ctx.fillStyle = color;
  ctx.fillText(label, textStart, y + 4);
  ctx.restore();
}

module.exports = {
  themeFromEnv,
  COLOR_BG,
  COLOR_SECTION,
  COLOR_BORDER,
  COLOR_TEXT,
  COLOR_MUTED,
  COLOR_GREEN,
  COLOR_RED,
  COLOR_YELLOW,
  DARK_THEME,
  LIGHT_THEME,
  HEADER_H,
  STATS_H,
  compact,
  formatFull,
  roundRect,
  section,
  rankChangeText,
  truncate,
  drawHeader,
  drawStatsBox,
  drawThresholdLine,
};
