import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from sim.scenarios import SCENARIOS, run_scenario


# One seed per scenario keeps the suite quick; `python -m sim` runs more.
@pytest.mark.parametrize("name", [name for name in SCENARIOS if name != "large"])
def test_simulated_tournament_has_no_problems(name):
    sim = asyncio.run(run_scenario(SCENARIOS[name], seed=0))
    assert not sim.issues, "\n".join(str(issue) for issue in sim.issues)


def test_a_run_leaves_the_db_path_and_random_module_as_it_found_them():
    path_before = db.DB_PATH
    random.seed(123)
    expected = random.random()
    random.seed(123)
    asyncio.run(run_scenario(SCENARIOS["smooth"], seed=0))
    assert db.DB_PATH == path_before
    assert random.random() == expected
