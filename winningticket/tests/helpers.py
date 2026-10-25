from bracket import Match, assign_played_rounds, build_feeder_index, generate_tree


def numbered(matches_by_id: dict[int, Match]) -> dict[int, Match]:
    """Number a hand-built fixture's played rounds, as db does when it loads a bracket."""
    assign_played_rounds(matches_by_id, build_feeder_index(list(matches_by_id.values())))
    return matches_by_id


def matches_from_pairs(team_pairs: list[tuple]) -> dict[int, Match]:
    """A bracket built from round-1 pairs the way db stores it: ids are spec positions."""
    specs = generate_tree(team_pairs)
    id_of = {(s.round, s.slot_index): i for i, s in enumerate(specs)}
    matches_by_id = {
        i: Match(
            id=i, round=s.round, slot_index=s.slot_index, team_a_id=s.team_a, team_b_id=s.team_b,
            feeds_into_match_id=id_of.get((s.feeds_into_round, s.feeds_into_slot_index)),
            feeds_into_side=s.feeds_into_side,
        )
        for i, s in enumerate(specs)
    }
    return numbered(matches_by_id)
