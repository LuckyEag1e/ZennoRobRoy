from bracket import Match, assign_played_rounds, build_feeder_index


def numbered(matches_by_id: dict[int, Match]) -> dict[int, Match]:
    """Number a hand-built fixture's played rounds, as db does when it loads a bracket."""
    assign_played_rounds(matches_by_id, build_feeder_index(list(matches_by_id.values())))
    return matches_by_id
