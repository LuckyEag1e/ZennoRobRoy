const { createCanvas } = require('@napi-rs/canvas');
const { COLOR_BG, COLOR_BORDER, COLOR_TEXT, COLOR_MUTED, COLOR_GREEN, COLOR_RED, compact, formatFull, section } = require('./memberCardShared');

const WIDTH = 700;
const PAD = 20;
const HEADER_H = 90;
const STATS_H = 108;
const CHART_H = 220;
const SECTION_GAP = 10;

function formatSigned(n) {
  return `${n >= 0 ? '+' : ''}${compact(n)}`;
}

function relativeTime(isoString) {
  if (!isoString) return 'unknown';
  const days = Math.floor((Date.now() - new Date(isoString).getTime()) / 86400000);
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  return `${days} days ago`;
}

function drawTrendChart(ctx, x, y, w, h, series, label) {
  ctx.fillStyle = COLOR_MUTED;
  ctx.font = '14px sans-serif';
  ctx.fillText(label, x, y);

  const chartTop = y + 14;
  const chartHeight = h - 14;

  if (series.length < 2) {
    ctx.fillStyle = COLOR_MUTED;
    ctx.font = '14px sans-serif';
    ctx.fillText('Not enough data yet — check back after a few more fetches', x, chartTop + chartHeight / 2);
    return;
  }

  const min = Math.min(...series);
  const max = Math.max(...series);
  const range = max - min || 1;

  const points = series.map((v, i) => ({
    x: x + (i / (series.length - 1)) * w,
    y: chartTop + chartHeight - ((v - min) / range) * chartHeight,
  }));

  ctx.beginPath();
  ctx.moveTo(points[0].x, chartTop + chartHeight);
  for (const p of points) ctx.lineTo(p.x, p.y);
  ctx.lineTo(points[points.length - 1].x, chartTop + chartHeight);
  ctx.closePath();
  ctx.fillStyle = 'rgba(124, 111, 242, 0.2)';
  ctx.fill();

  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);
  for (const p of points.slice(1)) ctx.lineTo(p.x, p.y);
  ctx.strokeStyle = COLOR_BORDER;
  ctx.lineWidth = 3;
  ctx.stroke();

  for (const p of points) {
    ctx.beginPath();
    ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
    ctx.fillStyle = COLOR_BORDER;
    ctx.fill();
  }
}

// profile: one row from club_friend_profile. history: readMemberHistory() rows.
function buildProfileImage(profile, circleId, history) {
  const height = PAD * 2 + HEADER_H + SECTION_GAP + STATS_H + SECTION_GAP + CHART_H;
  const canvas = createCanvas(WIDTH, height);
  const ctx = canvas.getContext('2d');

  ctx.fillStyle = COLOR_BG;
  ctx.fillRect(0, 0, WIDTH, height);

  const sectionW = WIDTH - PAD * 2;
  let y = PAD;

  section(ctx, PAD, y, sectionW, HEADER_H);
  ctx.fillStyle = COLOR_TEXT;
  ctx.font = 'bold 26px sans-serif';
  ctx.fillText(profile.name, PAD + 20, y + 40);
  ctx.fillStyle = COLOR_MUTED;
  ctx.font = '14px sans-serif';
  ctx.fillText(
    `Circle ${circleId} · joined ${relativeTime(profile.join_time)} · last seen ${relativeTime(profile.last_login_time)}`,
    PAD + 20,
    y + 68,
  );
  y += HEADER_H + SECTION_GAP;

  section(ctx, PAD, y, sectionW, STATS_H);
  const statCol1 = PAD + 20;
  const statCol2 = PAD + sectionW / 2 + 10;

  ctx.fillStyle = COLOR_MUTED;
  ctx.font = '14px sans-serif';
  ctx.fillText('Fan Count', statCol1, y + 24);
  ctx.fillText('Daily Gain', statCol2, y + 24);

  ctx.fillStyle = COLOR_TEXT;
  ctx.font = 'bold 18px sans-serif';
  ctx.fillText(formatFull(profile.fan_count ?? 0), statCol1, y + 46);
  ctx.fillStyle = (profile.daily_diff ?? 0) >= 0 ? COLOR_GREEN : COLOR_RED;
  ctx.fillText(formatSigned(profile.daily_diff ?? 0), statCol2, y + 46);

  ctx.fillStyle = COLOR_MUTED;
  ctx.font = '14px sans-serif';
  ctx.fillText('Daily Avg', statCol1, y + 76);
  ctx.fillText('Monthly Avg', statCol2, y + 76);

  ctx.fillStyle = COLOR_TEXT;
  ctx.font = 'bold 16px sans-serif';
  ctx.fillText(compact(profile.daily_average ?? 0), statCol1, y + 96);
  ctx.fillText(compact(profile.monthly_average ?? 0), statCol2, y + 96);

  y += STATS_H + SECTION_GAP;

  section(ctx, PAD, y, sectionW, CHART_H);
  const series = history.map((h) => h.fan_count);
  const chartLabel = series.length >= 2 ? `Fan Count Trend (${series.length} local snapshots)` : 'Fan Count Trend';
  drawTrendChart(ctx, PAD + 20, y + 20, sectionW - 40, CHART_H - 40, series, chartLabel);

  return canvas.toBuffer('image/png');
}

module.exports = { buildProfileImage };
