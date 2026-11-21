"""Select and schedule tests for each independent SQLGlot patch."""

import argparse
import json
import sys
from pathlib import Path
from time import process_time_ns

from impact import ImpactIndex, Selection
from round3_imports import collection_partition, import_candidates, registry_edit
from round3_price import Group, file_groups, price, verify_groups
from round3_runtime import BASELINE, BatchBudget, patch_runtime
from round3_slices import eligible_slices
from scheduler import fill_spare_capacity, schedule, schedule_cost, test_milliseconds

DATA = Path("/data")
INDEX = Path(__file__).with_name("impact_index.json.gz")
PARTITION_SEARCH_BUDGET = 2_000


class Schedules:
    def __init__(self, tests):
        self.tests = {test["number"]: test for test in tests}
        self.durations = {number: test_milliseconds(test) for number, test in self.tests.items()}
        self.full_key = frozenset(self.tests)
        workers = schedule(tests, search_budget=PARTITION_SEARCH_BUDGET)
        self.cache = {self.full_key: (workers, schedule_cost(workers, self.durations))}

    def evaluate(self, numbers):
        key = frozenset(numbers)
        if key not in self.cache:
            workers = schedule([self.tests[number] for number in sorted(key)], search_budget=PARTITION_SEARCH_BUDGET)
            self.cache[key] = workers, schedule_cost(workers, self.durations)
        return self.cache[key]

    def cost(self, numbers):
        return min(self.evaluate(numbers)[1], self.cache[self.full_key][1])


def refined_selection(index, selection, schedules, budget, args):
    applied = selection.applied
    if not applied or args.no_pricing or args.no_runtime or budget.allowed(1) < 0.15:
        return selection, []
    numbers = frozenset(test["number"] for test in selection.tests)
    current_cost = schedules.cost(numbers)
    if current_cost < 1850:
        return selection, []
    paths = list(applied)
    imports = (not args.no_import_analysis and bool(index.index.get("round3_imports"))
               and registry_edit(applied) is not None and current_cost > 5000)
    proposals = []
    if not args.no_slices:
        estimates = eligible_slices(paths, selection.tests, baseline=args.baseline_source)
        shared = []
        for test in selection.tests:
            if test["id"] in estimates:
                number = frozenset({test["number"]})
                proposals.append(Group("slice", number, number, round(1000 * estimates[test["id"]])))
                if test["file"] == "tests/test_executor.py":
                    shared.append(test["number"])
        if len(shared) > 1:
            numbers_with_shared_setup = frozenset(shared)
            proposals.append(Group("slice", numbers_with_shared_setup, numbers_with_shared_setup, 5000))
    # Full-file replay preserves original sibling test order and class setup.
    # Source edits with cross-file state need a stronger certificate.
    if not args.no_verification and all(path.startswith("tests/fixtures/") for path in paths):
        proposals.extend(file_groups(selection.tests, schedules.tests.values(), schedules.durations))
    audit = args.audit_collection and "full-suite" in selection.evidence
    if not imports and not audit and price(numbers, proposals, schedules.cost, set())[0] is None:
        return selection, []
    records = []
    try:
        with patch_runtime(args.baseline_source, applied, budget) as runtime:
            if imports:
                before = runtime.children_cpu()
                proof = import_candidates(index, applied, runtime.snapshot)
                record = {"kind": "cold-import", "status": "unresolved",
                          "cpu_ms": round(1000 * (runtime.children_cpu() - before))}
                if proof:
                    bits, reason = proof
                    replacement = [test for bit, test, _ in index.known if bit & bits] + index.unknown
                    retained = frozenset(test["number"] for test in replacement)
                    cost = schedules.cost(retained)
                    if cost <= current_cost:
                        records.append({**record, "status": "proved", "removed": sorted(numbers - retained),
                                        "actual_ms": current_cost - cost})
                        return Selection(replacement, [], {reason: 1}), records
                records.append(record)
            if audit and (report := runtime.collect()):
                partition = collection_partition(report, list(schedules.tests.values()))
                if partition:
                    failed_collection, successful_collection = partition
                    records.append({"kind": "collection", "status": "complete",
                                    "failed_collection": len(failed_collection),
                                    "collected_runtime_candidates": len(successful_collection)})
            nodeids = {number: test["id"] for number, test in schedules.tests.items()}
            retained, verification = verify_groups(numbers, proposals, schedules.cost, runtime, nodeids, paths)
            records.extend(verification)
    except (OSError, ValueError, KeyError):
        return selection, records
    if retained == numbers:
        return selection, records
    removed = numbers - retained
    required = [test for test in selection.tests if test["number"] in retained]
    nearby = [test for test in selection.nearby if test["number"] not in removed]
    return Selection(required, nearby, {**selection.evidence, "verified-pass": len(removed)}), records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--impact-index", type=Path, default=INDEX)
    parser.add_argument("--all-tests", action="store_true", help="schedule the full suite for every patch")
    parser.add_argument("--explain", action="store_true", help="write the selection evidence to stderr")
    parser.add_argument("--no-module-pass", action="store_true", help="disable the compiled module-tail proof")
    parser.add_argument("--no-integer-comparisons", action="store_true", help="disable per-test ordered comparisons")
    parser.add_argument("--no-numeric-comparisons", action="store_true", help="disable the exact-float extension")
    parser.add_argument("--no-numeric-escapes", action="store_true", help="disable per-key tokenizer escape readers")
    parser.add_argument("--baseline-source", type=Path, default=BASELINE)
    parser.add_argument("--no-semantics", action="store_true", help="disable whole-patch semantic analysis")
    parser.add_argument("--no-import-analysis", action="store_true", help="disable cold registry comparison")
    parser.add_argument("--no-control", action="store_true", help="disable new input-sensitive control proofs")
    parser.add_argument("--no-slices", action="store_true", help="disable query and fixture replay")
    parser.add_argument("--no-pricing", action="store_true", help="schedule without cost-based verification")
    parser.add_argument("--no-verification", action="store_true", help="disable complete pytest-file replay")
    parser.add_argument("--no-runtime", action="store_true", help="use static evidence only")
    parser.add_argument("--audit-collection", action="store_true", help="record collection errors on full fallback")
    parser.add_argument("--verification-budget", type=float, default=24, help="maximum batch replay wall seconds")
    args = parser.parse_args()
    tests = json.loads((args.data_dir / "tests.json").read_text(encoding="utf-8"))["tests"]

    index = None
    if not args.all_tests:
        try:
            index = ImpactIndex(args.impact_index, tests, module_pass=not args.no_module_pass,
                                integer_comparisons=not args.no_integer_comparisons,
                                numeric_comparisons=not args.no_numeric_comparisons,
                                numeric_escapes=not args.no_numeric_escapes,
                                semantics=not args.no_semantics, control=not args.no_control,
                                baseline=args.baseline_source)
        except (OSError, EOFError, ValueError, KeyError) as error:
            print(f"Impact index unavailable. Scheduling the full suite. {error}", file=sys.stderr)

    schedules = Schedules(tests)
    budget = BatchBudget(max(0, min(args.verification_budget, 140)))
    full_workers, full_cost = schedules.evaluate(schedules.full_key)
    for patch in sorted((args.data_dir / "commits").glob("*.patch")):
        started = process_time_ns() if args.explain else 0
        selection = index.selection(patch, tests) if index else Selection(tests, [], {"full-suite": True})
        records = []
        if index:
            selection, records = refined_selection(index, selection, schedules, budget, args)
        key = frozenset(test["number"] for test in selection.tests)
        workers, cost = schedules.evaluate(key)
        if key != schedules.full_key and cost >= full_cost:
            selection = Selection(tests, [], {**selection.evidence, "full-suite": "equal or lower simulated cost"})
            workers = full_workers
        workers = fill_spare_capacity(workers, selection.tests, selection.nearby)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
        if args.explain:
            selected = sum(map(len, workers))
            print(json.dumps({"commit": patch.stem, "selected": selected, "workers": len(workers),
                              "process_cpu_us": (process_time_ns() - started) // 1000,
                              "filled_spare_capacity": selected - len(selection.tests),
                              "evidence": selection.evidence, "verification": records},
                             separators=(",", ":")), file=sys.stderr)


if __name__ == "__main__":
    main()
