"""Select and schedule tests for each independent SQLGlot patch."""

import argparse
import json
import sys
from pathlib import Path

from impact import ImpactIndex, Selection
from scheduler import fill_spare_capacity, schedule, schedule_cost, test_milliseconds

DATA = Path("/data")
INDEX = Path(__file__).with_name("impact_index.json.gz")
PARTITION_SEARCH_BUDGET = 2_000


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--impact-index", type=Path, default=INDEX)
    parser.add_argument("--all-tests", action="store_true", help="schedule the full suite for every patch")
    parser.add_argument("--explain", action="store_true", help="write the selection evidence to stderr")
    args = parser.parse_args()
    tests = json.loads((args.data_dir / "tests.json").read_text(encoding="utf-8"))["tests"]

    index = None
    if not args.all_tests:
        try:
            index = ImpactIndex(args.impact_index, tests)
        except (OSError, EOFError, ValueError, KeyError) as error:
            print(f"Impact index unavailable. Scheduling the full suite. {error}", file=sys.stderr)

    durations = {test["number"]: test_milliseconds(test) for test in tests}
    full_key = tuple(sorted(durations))
    full_workers = schedule(tests, search_budget=PARTITION_SEARCH_BUDGET)
    full_cost = schedule_cost(full_workers, durations)
    schedules = {full_key: (full_workers, full_cost)}
    for patch in sorted((args.data_dir / "commits").glob("*.patch")):
        selection = index.selection(patch, tests) if index else Selection(tests, [], {"full-suite": True})
        key = tuple(sorted(test["number"] for test in selection.tests))
        if key not in schedules:
            workers = schedule(selection.tests, search_budget=PARTITION_SEARCH_BUDGET)
            schedules[key] = (workers, schedule_cost(workers, durations))
        workers, cost = schedules[key]
        if key != full_key and cost >= full_cost:
            selection = Selection(tests, [], {"full-suite": "equal or lower simulated cost"})
            workers = full_workers
        workers = fill_spare_capacity(workers, selection.tests, selection.nearby)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
        if args.explain:
            selected = sum(map(len, workers))
            print(json.dumps({"commit": patch.stem, "selected": selected, "workers": len(workers),
                              "filled_spare_capacity": selected - len(selection.tests),
                              "evidence": selection.evidence}, separators=(",", ":")), file=sys.stderr)


if __name__ == "__main__":
    main()
