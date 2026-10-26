"""python -m sim [--scenario NAME ...] [--seeds N | --seed N] [--transcript]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter

from sim.scenarios import SCENARIOS, run_scenario


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m sim", description="Play simulated tournaments against the real cogs.")
    parser.add_argument("--scenario", action="append", choices=sorted(SCENARIOS),
                        help="scenario to run (repeatable; default: all)")
    parser.add_argument("--seeds", type=int, default=5, help="how many seeds to run each scenario with (default: 5)")
    parser.add_argument("--seed", type=int, help="run just this one seed")
    parser.add_argument("--transcript", action="store_true", help="print every step, not only the problems")
    parser.add_argument("--coverage", action="store_true",
                        help="after the runs, show how often each command ran at each tournament stage")
    parser.add_argument("--list", action="store_true", help="list the scenarios and exit")
    args = parser.parse_args()

    if args.list:
        for scenario in SCENARIOS.values():
            print(f"{scenario.name:10} {scenario.description}")
        return 0

    # The cogs log expected failures (like a missing channel) as warnings; keep the output to the results.
    logging.basicConfig(level=logging.ERROR)
    seeds = [args.seed] if args.seed is not None else list(range(args.seeds))
    total_issues = 0
    coverage = Counter()
    every_command = set()
    for name in args.scenario or sorted(SCENARIOS):
        for seed in seeds:
            sim = asyncio.run(run_scenario(SCENARIOS[name], seed))
            total_issues += len(sim.issues)
            coverage.update(sim.coverage)
            every_command |= set(sim.commands)
            print(f"{name} seed {seed}: {sim.step} steps, {len(sim.issues)} problem(s)")
            if args.transcript:
                print("\n".join(sim.transcript))
            for issue in sim.issues:
                print(f"  - {issue}")
    if args.coverage:
        print_coverage(coverage, every_command)
    return 1 if total_issues else 0


def print_coverage(coverage: Counter, every_command: set[str]) -> None:
    stages = ["-", "registration", "setup", "predictions_open", "in_progress", "completed", "concluded"]
    print(f"\n{'command':26}" + "".join(f"{(stage if stage != '-' else 'no tournament')[:12]:>13}" for stage in stages))
    for command in sorted(every_command):
        counts = [coverage[(command, stage)] for stage in stages]
        print(f"/{command:25}" + "".join(f"{count or '.':>13}" for count in counts))


if __name__ == "__main__":
    sys.exit(main())
