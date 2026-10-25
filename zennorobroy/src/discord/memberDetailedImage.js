const { createCanvas } = require('@napi-rs/canvas');
const { computeMemberStats } = require('../memberStats');
const { getInactivityCredit } = require('../config');
const {
  DARK_THEME,
  HEADER_H,
  STATS_H,
  compact,
  section,
  truncate,
  drawHeader,
  drawStatsBox,
  drawThresholdLine,
} = require('./memberCardShared');

const WIDTH = 820;
const ROW_HEIGHT = 28;
const PAD = 20;
const SECTION_GAP = 10;

function effectiveAvg(member, creditDays) {
  if (member.days === 0) return 0;
  const effectiveDays = Math.max(1, member.days - creditDays);
  return member.gain / effectiveDays;
}

// e.g. 7 tracked days with 3 approved-inactivity days credited -> "7(-3)"
function formatDays(member, creditDays) {
  if (member.days === 0) return '–';
  return creditDays > 0 ? `${member.days}(-${creditDays})` : `${member.days}`;
}

function formatSigned(n) {
  return `${n >= 0 ? '+' : ''}${compact(n)}`;
}

function buildMemberDetailedImage(data, circleId, quotaPerDay, goalPerDay, guildId, theme = DARK_THEME) {
  const club = data.club && data.club[0];
  if (!club) {
    throw new Error(`No club data found for circle_id ${circleId}`);
  }

  const { members, day, periodGain } = computeMemberStats(data);
  const credits = new Map(members.map((m) => [m.id, getInactivityCredit(guildId, m.id)]));

  const tableHeaderH = 32;
  const tableH = tableHeaderH + members.length * ROW_HEIGHT;
  const footerH = 50;

  const height = PAD * 2 + HEADER_H + SECTION_GAP + STATS_H + SECTION_GAP + tableH + SECTION_GAP + footerH;

  const canvas = createCanvas(WIDTH, height);
  const ctx = canvas.getContext('2d');

  ctx.fillStyle = theme.bg;
  ctx.fillRect(0, 0, WIDTH, height);

  const sectionW = WIDTH - PAD * 2;
  let y = PAD;

  drawHeader(ctx, PAD, y, sectionW, club, circleId, day, theme);
  y += HEADER_H + SECTION_GAP;

  const quotaEnabled = quotaPerDay > 0;
  const expectedGain = quotaPerDay * day;
  const totalCreditDays = quotaEnabled
    ? members.reduce((sum, m) => sum + Math.min(credits.get(m.id) || 0, day), 0)
    : 0;
  const buffer = quotaEnabled ? periodGain - (expectedGain * members.length - quotaPerDay * totalCreditDays) : null;

  const goalEnabled = goalPerDay > 0;
  const goalExpectedGain = goalPerDay * day;

  drawStatsBox(ctx, PAD, y, sectionW, {
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
    theme,
  });
  y += STATS_H + SECTION_GAP;

  // --- Table ---
  section(ctx, PAD, y, sectionW, tableH, theme);

  const colRank = PAD + 20;
  const colName = PAD + 55;
  const colDaysRight = PAD + sectionW - 20;
  const colLatestRight = colDaysRight - 90;
  const colGainRight = colLatestRight - 90;
  const colAvgRight = colGainRight - 90;
  const nameMaxWidth = colAvgRight - 90 - colName;

  ctx.font = 'bold 14px sans-serif';
  ctx.fillStyle = theme.muted;
  ctx.fillText('#', colRank, y + 22);
  ctx.fillText('Trainer', colName, y + 22);
  ctx.textAlign = 'right';
  ctx.fillText('Avg', colAvgRight, y + 22);
  ctx.fillText('Gain', colGainRight, y + 22);
  ctx.fillText('+/-', colLatestRight, y + 22);
  ctx.fillText('Days', colDaysRight, y + 22);
  ctx.textAlign = 'left';

  let rowY = y + tableHeaderH;

  // Dividers between members meeting a threshold and those falling short
  // (members are sorted by avg descending, so this is just the boundary index).
  // Approved-inactivity credit is applied here only, not to the sort order above.
  // Quota is the hard requirement (bold/red); goal is an aspirational stretch
  // target (subtle/dashed/yellow) and is drawn first so quota wins if they coincide.
  const quotaCutoff =
    quotaPerDay > 0 ? members.findIndex((m) => effectiveAvg(m, credits.get(m.id) || 0) < quotaPerDay) : -1;
  const goalCutoff =
    goalPerDay > 0 ? members.findIndex((m) => effectiveAvg(m, credits.get(m.id) || 0) < goalPerDay) : -1;

  members.forEach((m, i) => {
    if (i === goalCutoff && i > 0) {
      drawThresholdLine(ctx, PAD + 12, PAD + sectionW - 12, rowY, { color: theme.yellow, subtle: true, label: 'Goal' });
    }
    if (i === quotaCutoff && i > 0) {
      drawThresholdLine(ctx, PAD + 12, PAD + sectionW - 12, rowY, { color: theme.red });
    }

    const rankFromBottom = members.length - i;
    let color = theme.text;
    if (rankFromBottom <= 2) color = theme.red;
    else if (rankFromBottom <= 4) color = theme.yellow;

    const textY = rowY + 19;
    ctx.fillStyle = color;
    ctx.font = i < 3 ? 'bold 19px sans-serif' : '15px sans-serif';
    ctx.fillText(`${i + 1}`, colRank, textY);
    ctx.fillText(truncate(ctx, m.name, nameMaxWidth), colName, textY);

    const credit = credits.get(m.id) || 0;

    ctx.textAlign = 'right';
    ctx.fillText(m.days > 0 ? compact(m.avg) : '–', colAvgRight, textY);
    ctx.fillText(m.days > 0 ? compact(m.gain) : '–', colGainRight, textY);
    ctx.fillText(m.days > 0 ? formatSigned(m.latestGain) : '–', colLatestRight, textY);
    ctx.fillText(formatDays(m, credit), colDaysRight, textY);
    ctx.textAlign = 'left';

    rowY += ROW_HEIGHT;
  });

  y += tableH + SECTION_GAP;

  // --- Footer ---
  section(ctx, PAD, y, sectionW, footerH, theme);
  ctx.fillStyle = theme.muted;
  ctx.font = '13px sans-serif';
  const footerText = `Rendered locally · ${new Date().toISOString().slice(0, 16).replace('T', ' ')} UTC`;
  const footerWidth = ctx.measureText(footerText).width;
  ctx.fillText(footerText, PAD + sectionW / 2 - footerWidth / 2, y + footerH / 2 + 5);

  return canvas.toBuffer('image/png');
}

module.exports = { buildMemberDetailedImage };
