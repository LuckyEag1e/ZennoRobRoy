const BASE_URL = 'https://api.chronogenesis.net';

const USER_AGENT =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36';

async function fetchClubProfile(circleId, apiKey) {
  const res = await fetch(`${BASE_URL}/club_profile?circle_id=${circleId}`, {
    headers: {
      Authorization: apiKey,
      'User-Agent': USER_AGENT,
    },
  });

  if (!res.ok) {
    throw new Error(`Chrono Genesis API returned ${res.status} ${res.statusText}`);
  }

  return res.json();
}

module.exports = { fetchClubProfile };
