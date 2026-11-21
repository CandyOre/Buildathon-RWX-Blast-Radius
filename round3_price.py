"""Price verification from actual schedules, then apply only complete evidence."""

from collections import defaultdict
from dataclasses import dataclass


MINIMUM_GAIN_MS = 250
COLLECTION_MS = 1150
MAX_TRIALS_PER_PATCH = 2


@dataclass(frozen=True)
class Group:
    kind: str
    selected: frozenset
    requested: frozenset
    expected_ms: int


def file_groups(candidates, catalog, durations):
    files = defaultdict(set)
    selected = defaultdict(set)
    for test in catalog:
        files[test["file"]].add(test["number"])
    for test in candidates:
        selected[test["file"]].add(test["number"])
    groups = []
    for name, numbers in selected.items():
        if name == "tests/test_sqlglotc.py":
            # The setup test resolves the checkout path through its source file.
            continue
        peers = files[name]
        cpu = COLLECTION_MS + sum(max(6, durations[number]) for number in peers) * 3 // 2
        groups.append(Group("pytest", frozenset(numbers), frozenset(peers), cpu))
    return groups


def price(candidates, proposals, cost_of, attempted, failed=frozenset()):
    scheduled = cost_of(candidates)
    best = (0, None, 0)
    for proposal in proposals:
        removable = (proposal.selected & candidates) - failed
        if proposal in attempted or not removable:
            continue
        gain = scheduled - cost_of(candidates - removable)
        margin = gain - proposal.expected_ms
        if margin >= MINIMUM_GAIN_MS and margin > best[0]:
            best = margin, proposal, gain
    _, proposal, gain = best
    return proposal, gain


def verify_groups(candidates, proposals, cost_of, runtime, nodeids, paths, *, attempt_limit=MAX_TRIALS_PER_PATCH):
    attempted = set()
    failed = set()
    records = []
    for _ in range(attempt_limit):
        proposal, possible = price(candidates, proposals, cost_of, attempted, failed)
        if proposal is None:
            break
        attempted.add(proposal)
        request = [nodeids[number] for number in proposal.requested]
        # The cost ceiling includes a margin for launching the process. A
        # timeout is useful evidence for neither passing nor removal.
        cap = min(7, (possible - MINIMUM_GAIN_MS) / 1000,
                  max(1.5, 1.4 * proposal.expected_ms / 1000))
        receipt = runtime.verify(proposal.kind, request, paths, timeout=cap)
        failed.update(number for number in proposal.selected if nodeids[number] in receipt.failed)
        accepted = {number for number in (proposal.selected & candidates) - failed if nodeids[number] in receipt.passed}
        remaining = candidates - accepted
        claimed = cost_of(candidates) - cost_of(remaining)
        records.append({
            "kind": proposal.kind, "status": receipt.status, "expected_cpu_ms": proposal.expected_ms,
            "possible_ms": possible, "actual_ms": claimed, "cpu_ms": round(receipt.cpu * 1000),
            "elapsed_ms": round(receipt.elapsed * 1000), "removed": sorted(accepted),
            "failed": sorted(number for number in proposal.selected if nodeids[number] in receipt.failed),
            "detail": receipt.detail,
        })
        candidates = remaining
    return candidates, records
