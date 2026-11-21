"""Assign selected tests to workers under the Blast Radius scoring cost."""

import heapq
import math
from itertools import repeat
from typing import Iterator, Optional

MAXIMUM_WORKERS = 8
WORKER_COST_MILLISECONDS = 2_000


class _SearchLimit(Exception):
    """The caller's allowance for exploring partition states is exhausted."""


def test_milliseconds(test: dict) -> int:
    return math.floor(test["seconds"] * 1000 + 0.5)


def schedule_cost(workers: list[list[int]], durations: dict[int, int]) -> int:
    loads = [sum(durations[number] for number in worker) for worker in workers if worker]
    return max(loads, default=0) + WORKER_COST_MILLISECONDS * len(loads)


def fill_spare_capacity(workers: list[list[int]], tests: list[dict], nearby: list[dict]) -> list[list[int]]:
    """Add related tests without increasing worker count or busiest load."""
    if not workers or not nearby:
        return workers
    durations = {test["number"]: test_milliseconds(test) for test in tests}
    loads = [(sum(durations[number] for number in worker), index) for index, worker in enumerate(workers)]
    limit = max(load for load, _ in loads)
    heapq.heapify(loads)
    result = [worker.copy() for worker in workers]
    scheduled = {number for worker in workers for number in worker}
    for duration, number in sorted((test_milliseconds(test), test["number"]) for test in nearby):
        load, index = loads[0]
        if load + duration > limit:
            break
        if number not in scheduled:
            result[index].append(number)
            scheduled.add(number)
            heapq.heapreplace(loads, (load + duration, index))
    return result


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
    ordered_tests: list[tuple[int, int]], worker_count: int, limit: int, search_steps: Iterator[None]
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
        if next(search_steps, False) is False:
            raise _SearchLimit

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


def schedule(tests: list[dict], *, search_budget: Optional[int] = None) -> list[list[int]]:
    """Minimize cost and favor fewer workers, bounding exact search if requested."""
    if not tests:
        return []

    ordered_tests = sorted(
        ((test["number"], test_milliseconds(test)) for test in tests),
        key=lambda test: (-test[1], test[0]),
    )
    total_duration = sum(duration for _, duration in ordered_tests)
    longest_test = ordered_tests[0][1]
    best_workers = [[number for number, _ in ordered_tests]]
    best_cost = total_duration + WORKER_COST_MILLISECONDS
    search_steps = repeat(None) if search_budget is None else repeat(None, search_budget)
    searches_remaining = search_budget != 0

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
        while searches_remaining and lower_bound <= upper_bound:
            limit = (lower_bound + upper_bound) // 2
            try:
                workers = _pack(ordered_tests, worker_count, limit, search_steps)
            except _SearchLimit:
                searches_remaining = False
                break
            if workers is None:
                lower_bound = limit + 1
            else:
                best_workers = workers
                best_cost = limit + overhead
                upper_bound = limit - 1

    return best_workers
