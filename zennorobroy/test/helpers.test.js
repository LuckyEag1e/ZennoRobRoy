// Small pure helpers used by the image renderers and /share_parent.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { buildFactorIndex, resolveFactor, resolveCardName } = require('../src/discord/factorResolve');
const { mergeSparks } = require('../src/discord/parentSparkExtract');
const { compact, formatFull, themeFromEnv, DARK_THEME } = require('../src/discord/memberCardShared');

test('resolveFactor splits the star digit off the base id', () => {
  const index = buildFactorIndex([{ id: '320', text: 'Mile' }]);
  assert.deepEqual(resolveFactor(3201, index), { label: 'Mile', stars: 1 });
  assert.deepEqual(resolveFactor('3203', index), { label: 'Mile', stars: 3 });
  assert.deepEqual(resolveFactor('9992', index), { label: 'Unknown Spark (#9992)', stars: 2 });
});

test('resolveCardName drops the 2-digit costume suffix', () => {
  const names = { 1003: { name: 'Tokai Teio' } };
  assert.equal(resolveCardName(100301, names), 'Tokai Teio');
  assert.equal(resolveCardName(999901, names), 'Card #999901');
  assert.equal(resolveCardName(null, names), null);
});

test('mergeSparks sums stars per name case-insensitively and counts contributors', () => {
  const merged = mergeSparks([
    [{ label: 'Speed', stars: 3 }, { label: 'Mile', stars: 1 }],
    [{ label: 'speed', stars: 2 }, null, { label: '', stars: 1 }],
  ]);
  assert.deepEqual(merged, [
    { label: 'Speed', totalStars: 5, contributors: 2 },
    { label: 'Mile', totalStars: 1, contributors: 1 },
  ]);
});

test('compact formats K/M with sign', () => {
  assert.equal(compact(999), '999');
  assert.equal(compact(1500), '2K');
  assert.equal(compact(-25_400), '-25K');
  assert.equal(compact(1_234_567), '1.23M');
});

test('formatFull adds thousands separators and rounds', () => {
  assert.equal(formatFull(1234567.6), '1,234,568');
});

test('themeFromEnv takes valid hex overrides and ignores invalid ones', (t) => {
  t.mock.method(console, 'warn', () => {});
  process.env.TEST_COLOR_BG = '#abcdef';
  process.env.TEST_COLOR_RED = 'red';
  try {
    const theme = themeFromEnv('TEST', DARK_THEME);
    assert.equal(theme.bg, '#abcdef');
    assert.equal(theme.red, DARK_THEME.red);
    assert.equal(theme.text, DARK_THEME.text);
    assert.equal(console.warn.mock.callCount(), 1);
  } finally {
    delete process.env.TEST_COLOR_BG;
    delete process.env.TEST_COLOR_RED;
  }
});
