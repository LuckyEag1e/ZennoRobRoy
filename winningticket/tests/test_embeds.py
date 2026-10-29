import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bracket import UserScore
from embeds import EMBED_DESCRIPTION_LIMIT, render_leaderboard_embed


def test_a_long_leaderboard_stays_within_discords_limit():
    scores = [UserScore(user_id=10**17 + n, points=500 - n, correct_picks=40, possible_picks=63) for n in range(300)]
    embed = render_leaderboard_embed("Cup", scores)
    assert len(embed.description) <= EMBED_DESCRIPTION_LIMIT
    assert embed.description.endswith("more")


def test_tied_scores_share_a_place_on_the_leaderboard():
    scores = [UserScore(1, 5, 2, 3), UserScore(2, 5, 2, 3), UserScore(3, 1, 1, 3)]
    lines = render_leaderboard_embed("Cup", scores).description.splitlines()[1:]
    assert [line.split()[0] for line in lines] == ["**1.**", "**1.**", "**3.**"]
