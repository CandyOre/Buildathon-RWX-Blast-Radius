"""Run every test on every commit with a cost-aware worker assignment."""

import argparse
import heapq
import json
import math
import sys
from pathlib import Path
from typing import Optional

DATA = Path("/data")
MAXIMUM_WORKERS = 8
WORKER_COST_MILLISECONDS = 2_000


def _split(ordered_tests: list[tuple[int, int]], worker_count: int) -> tuple[list[list[int]], int]:
    """Assign longest tests first and return the workers and busiest load."""
    # Start each worker with a test so even a zero-duration test occupies a worker.
    first_tests = ordered_tests[:worker_count]
    workers = [[number] for number, _ in first_tests]
    loads = [(duration, index) for index, (_, duration) in enumerate(first_tests)]
    heapq.heapify(loads)
    for number, duration in ordered_tests[worker_count:]:
        load, index = loads[0]
        workers[index].append(number)
        heapq.heapreplace(loads, (load + duration, index))
    return workers, max(load for load, _ in loads)


def _pack(
    ordered_tests: list[tuple[int, int]], worker_count: int, limit: int
) -> Optional[list[list[int]]]:
    """Find a partition whose busiest worker stays within limit, if one exists."""
    # An explicit stack also handles manifests longer than Python's recursion limit.
    stack = [(0, (limit,) * worker_count, None)]
    seen = set()
    while stack:
        test_index, capacities, placement = stack.pop()
        if test_index == len(ordered_tests):
            workers: list[list[int]] = [[] for _ in range(worker_count)]
            for number, _ in reversed(ordered_tests):
                worker, placement = placement
                workers[worker].append(number)
            # Filling empty workers from the largest group cannot increase the load.
            for worker in workers:
                if not worker:
                    worker.append(max(workers, key=len).pop())
            for worker in workers:
                worker.reverse()
            return workers

        # Worker labels do not affect feasibility. Search each capacity state once.
        state = (test_index, tuple(sorted(capacities)))
        if state in seen:
            continue
        seen.add(state)

        _, duration = ordered_tests[test_index]
        tried = set()
        # Stack order tries the tightest fit first.
        for worker in sorted(range(worker_count), key=capacities.__getitem__, reverse=True):
            capacity = capacities[worker]
            if capacity < duration:
                break
            if capacity in tried:
                continue
            tried.add(capacity)
            remaining = capacities[:worker] + (capacity - duration,) + capacities[worker + 1:]
            stack.append((test_index + 1, remaining, (worker, placement)))

    return None


def schedule(tests: list[dict]) -> list[list[int]]:
    """Minimize the scorer's cost, breaking ties in favor of fewer workers."""
    if not tests:
        return []

    ordered_tests = sorted(
        ((test["number"], math.floor(test["seconds"] * 1000 + 0.5)) for test in tests),
        key=lambda test: (-test[1], test[0]),
    )
    total_duration = sum(duration for _, duration in ordered_tests)
    longest_test = ordered_tests[0][1]
    best_workers = [[number for number, _ in ordered_tests]]
    best_cost = total_duration + WORKER_COST_MILLISECONDS

    for worker_count in range(2, min(MAXIMUM_WORKERS, len(ordered_tests)) + 1):
        overhead = worker_count * WORKER_COST_MILLISECONDS
        average_load = (total_duration + worker_count - 1) // worker_count
        lower_bound = max(longest_test, average_load)
        if lower_bound + overhead >= best_cost:
            continue

        workers, busiest_load = _split(ordered_tests, worker_count)
        cost = busiest_load + overhead
        if cost < best_cost:
            best_workers = workers
            best_cost = cost

        # Only search loads that could beat the best score already found.
        upper_bound = best_cost - overhead - 1
        while lower_bound <= upper_bound:
            limit = (lower_bound + upper_bound) // 2
            workers = _pack(ordered_tests, worker_count, limit)
            if workers is None:
                lower_bound = limit + 1
            else:
                best_workers = workers
                best_cost = limit + overhead
                upper_bound = limit - 1

    return best_workers


def main() -> None:
    parser = argparse.ArgumentParser(description="Select tests for each input patch.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA,
        help="directory containing tests.json and commits/",
    )
    data_dir = parser.parse_args().data_dir

    tests = json.loads((data_dir / "tests.json").read_text(encoding="utf-8"))["tests"]
    workers = schedule(tests)
    for patch in sorted((data_dir / "commits").glob("*.patch")):
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    print(f"selected {len(tests)} tests on {len(workers)} workers per commit", file=sys.stderr)


if __name__ == "__main__":
    main()
