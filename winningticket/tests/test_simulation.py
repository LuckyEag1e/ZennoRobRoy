import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from sim.scenarios import SCENARIOS, run_scenario


# One seed per scenario keeps the suite quick; `python -m sim` runs more.
@pytest.mark.parametrize("name", [name for name in SCENARIOS if name != "large"])
def test_simulated_tournament_has_no_problems(name):
    sim = asyncio.run(run_scenario(SCENARIOS[name], seed=0))
    assert not sim.issues, "\n".join(str(issue) for issue in sim.issues)
