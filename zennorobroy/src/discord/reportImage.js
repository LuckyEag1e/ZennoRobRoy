const { createCanvas } = require('@napi-rs/canvas');

const WIDTH = 900;
const HEIGHT = 580;

const COLOR_BG = '#1e1f22';
const COLOR_CARD = '#2b2d31';
const COLOR_TEXT = '#f2f3f5';
const COLOR_MUTED = '#96989d';
const COLOR_ACCENT = '#5865f2';
const COLOR_GREEN = '#57f287';
const COLOR_RED = '#ed4245';

function formatNumber(n) {
  return new Intl.NumberFormat('en-US').format(n);
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

function drawStat(ctx, x, y, label, value, valueColor) {
  ctx.fillStyle = COLOR_MUTED;
  ctx.font = '20px sans-serif';
  ctx.fillText(label, x, y);

  ctx.fillStyle = valueColor || COLOR_TEXT;
  ctx.font = 'bold 30px sans-serif';
  ctx.fillText(value, x, y + 36);
}

function drawTrendChart(ctx, x, y, w, h, series, label) {
  ctx.fillStyle = COLOR_MUTED;
  ctx.font = '18px sans-serif';
  ctx.fillText(label, x, y);

  const chartTop = y + 16;
  const chartHeight = h - 16;

  if (series.length < 2) {
    ctx.fillStyle = COLOR_MUTED;
    ctx.font = '16px sans-serif';
    ctx.fillText('Not enough data yet', x, chartTop + chartHeight / 2);
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
  ctx.fillStyle = 'rgba(88, 101, 242, 0.2)';
  ctx.fill();

  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);
  for (const p of points.slice(1)) ctx.lineTo(p.x, p.y);
  ctx.strokeStyle = COLOR_ACCENT;
  ctx.lineWidth = 3;
  ctx.stroke();

  for (const p of points) {
    ctx.beginPath();
    ctx.arc(p.x, p.y, 3.5, 0, Math.PI * 2);
    ctx.fillStyle = COLOR_ACCENT;
    ctx.fill();
  }
}

// rank_diff/rank_gain = old_rank - new_rank; positive means the rank
// number went down (improved), negative means it went up (worse).
function rankChangeText(diff) {
  if (diff > 0) return `▲ ${formatNumber(diff)}`;
  if (diff < 0) return `▼ ${formatNumber(Math.abs(diff))}`;
  return '– no change';
}

function buildReportImage(data, circleId, history) {
  const club = data.club && data.club[0];
  if (!club) {
    throw new Error(`No club data found for circle_id ${circleId}`);
  }

  const canvas = createCanvas(WIDTH, HEIGHT);
  const ctx = canvas.getContext('2d');

  ctx.fillStyle = COLOR_BG;
  ctx.fillRect(0, 0, WIDTH, HEIGHT);

  roundRect(ctx, 20, 20, WIDTH - 40, HEIGHT - 40, 16);
  ctx.fillStyle = COLOR_CARD;
  ctx.fill();

  const rankDiff = club.rank_diff ?? 0;
  const rankColor = rankDiff > 0 ? COLOR_GREEN : rankDiff < 0 ? COLOR_RED : COLOR_TEXT;

  ctx.fillStyle = COLOR_TEXT;
  ctx.font = 'bold 38px sans-serif';
  ctx.fillText(club.name, 50, 80);

  ctx.fillStyle = COLOR_MUTED;
  ctx.font = '18px sans-serif';
  ctx.fillText(`Circle ID: ${circleId}  •  Updated ${new Date(club.updated_at || Date.now()).toISOString().slice(0, 16).replace('T', ' ')} UTC`, 50, 108);

  const statsY = 175;
  const col1 = 50;
  const col2 = 340;
  const col3 = 630;

  drawStat(ctx, col1, statsY, 'Rank', `#${formatNumber(club.rank)}`);
  drawStat(ctx, col2, statsY, 'Rank Change', rankChangeText(rankDiff), rankColor);
  drawStat(ctx, col3, statsY, 'Members', `${club.member_num}`);

  drawStat(ctx, col1, statsY + 90, 'Fan Count', formatNumber(club.fan_count));
  drawStat(ctx, col2, statsY + 90, 'Daily Gain', formatNumber(club.daily_diff ?? 0));
  drawStat(ctx, col3, statsY + 90, 'Monthly Avg', formatNumber(club.monthly_average ?? 0));

  const series = history && history.length >= 2 ? history.map((h) => h.fan_count) : [];
  const chartLabel = series.length >= 2 ? `Fan Count Trend (${series.length} local snapshots)` : 'Fan Count Trend';
  drawTrendChart(ctx, col1, statsY + 190, WIDTH - 100, 150, series, chartLabel);

  return canvas.toBuffer('image/png');
}

module.exports = { buildReportImage };
