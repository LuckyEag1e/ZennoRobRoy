// Builds a base-id -> entry lookup from a factor table fetched via
// umaMoeApi.fetchFactorTable().
function buildFactorIndex(factorTable) {
  return new Map(factorTable.map((f) => [f.id, f]));
}

// Resolves a single raw factor ID (e.g. "3201") to { label, stars }. uma.moe
// factor IDs are `<base id><star level digit>` — base "320" ("Mile") + level 1.
// Falls back to a labeled-unknown entry (still rendered, just not a real name)
// rather than dropping the spark silently — this only happens for factor IDs
// added since uma.moe's resource was last regenerated.
function resolveFactor(factorId, factorIndex) {
  const id = String(factorId);
  const base = id.slice(0, -1);
  const stars = Number(id.slice(-1));
  const entry = factorIndex.get(base);

  return entry ? { label: entry.text, stars } : { label: `Unknown Spark (#${id})`, stars };
}

// Resolves a card/costume ID (e.g. inheritance.main_parent_id) to a character
// name, using the { [4-digit base id]: { name, skins } } shape from
// umaMoeApi.fetchCharacterNames().
function resolveCardName(cardId, characterNames) {
  if (cardId == null) return null;
  const baseId = String(cardId).slice(0, -2);
  return characterNames[baseId]?.name ?? `Card #${cardId}`;
}

module.exports = { buildFactorIndex, resolveFactor, resolveCardName };
