"""Replay changed fixture units through SQLGlot's pinned pytest assertions."""

import argparse
import ast
import json
import os
import sys
from contextlib import contextmanager
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch


FIXTURES = "tests/fixtures/"
OPTIMIZER = "tests/test_optimizer.py"
EXECUTOR = "tests/test_executor.py"
TPCH = "optimizer/tpc-h/tpc-h.sql"
TPCDS = "optimizer/tpc-ds/tpc-ds.sql"
EXECUTOR_FIXTURE = {
    f"{EXECUTOR}::TestExecutor::test_execute_tpch": (TPCH, "tpch_sqls"),
    f"{EXECUTOR}::TestExecutor::test_execute_tpcds": (TPCDS, "tpcds_sqls"),
    f"{EXECUTOR}::TestExecutor::test_optimized_tpch": (TPCH, "tpch_sqls"),
}


@lru_cache(maxsize=4)
def optimizer_fixtures(source):
    """Read fixed test methods rather than matching fixture names to patch IDs."""
    tree = ast.parse((source / OPTIMIZER).read_bytes())
    owner = next(stmt for stmt in tree.body if isinstance(stmt, ast.ClassDef) and stmt.name == "TestOptimizer")
    methods = {}
    for method in owner.body:
        if not isinstance(method, ast.FunctionDef) or not method.name.startswith("test_"):
            continue
        files = {}
        for call in ast.walk(method):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "check_file"):
                continue
            if not (len(call.args) == 2 and isinstance(call.func.value, ast.Name) and call.func.value.id == "self"
                    and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)):
                break
            filename = f"optimizer/{call.args[0].value}.sql"
            keywords = {kw.arg: kw.value for kw in call.keywords}
            if None in keywords or "only" in keywords or filename in files:
                break
            execute = keywords.get("execute")
            if execute is not None and not (isinstance(execute, ast.Constant) and execute.value is False):
                break
            files[filename] = execute is not None
        else:
            if files:
                methods[f"{OPTIMIZER}::TestOptimizer::{method.name}"] = files
    return methods


def eligible_slices(paths, tests, baseline=Path("/opt/sqlglot")):
    """Return recognized fixture-owned node IDs and conservative CPU estimates."""
    paths = tuple(paths)
    if not paths or any(not path.startswith(f"{FIXTURES}optimizer/") or not path.endswith(".sql") for path in paths):
        return {}
    try:
        optimizer = optimizer_fixtures(Path(baseline))
    except (OSError, SyntaxError, StopIteration):
        return {}
    fixtures = {path.removeprefix(FIXTURES) for path in paths}
    executor = EXECUTOR_FIXTURE if fixtures <= {TPCH, TPCDS} else {}
    result = {}
    for test in tests:
        nodeid = test["id"] if isinstance(test, dict) else test
        if nodeid in executor:
            result[nodeid] = 4.5
        elif fixtures.intersection(optimizer.get(nodeid, ())):
            result[nodeid] = 2.8
    return result


@contextmanager
def substitute(owner, name, value):
    previous = getattr(owner, "__dict__", {}).get(name, MISSING)
    setattr(owner, name, value)
    try:
        yield
    finally:
        if previous is MISSING:
            delattr(owner, name)
        else:
            setattr(owner, name, previous)


MISSING = object()


def pair_key(pair):
    meta, sql, expected = pair
    return tuple(meta.items()), sql, expected


def changed_units(before, after):
    """Align complete input/expectation pairs, including metadata, after lexing all bytes."""
    previous = [pair_key(pair) for pair in before]
    current = [pair_key(pair) for pair in after]
    matcher = SequenceMatcher(a=previous, b=current, autojunk=False)
    unchanged = {after_start + offset for _, after_start, size in matcher.get_matching_blocks()
                 for offset in range(size)}
    return [i for i in range(len(after)) if i not in unchanged]


def read_only_queries(nodeid, units):
    import sqlglot
    from sqlglot import exp

    unstable = (exp.DML, exp.DDL, exp.Into, exp.Command, exp.Anonymous, exp.Rand, exp.Uuid,
                exp.CurrentTimestamp, exp.CurrentDate, exp.CurrentTime, exp.CurrentUser)
    for meta, sql, expected in units:
        if nodeid.endswith("::test_optimized_tpch"):
            queries = (sql, expected)
        elif meta.get("execute"):
            # Upstream mp_execute uses truthiness of the metadata string.
            queries = (sql,)
        else:
            continue
        for query in queries:
            expressions = sqlglot.parse(query)
            if (len(expressions) != 1 or not isinstance(expressions[0], exp.Query)
                    or expressions[0].find(*unstable)):
                return False
    return True


def prepare(baseline, workspace, paths, targets, helpers):
    optimizer = optimizer_fixtures(baseline)
    fixtures = {path.removeprefix(FIXTURES) for path in paths}
    proposed = set(eligible_slices(paths, targets, baseline))
    loaded = {}
    executor_requested = any(node in EXECUTOR_FIXTURE for node in proposed)
    required = fixtures | ({TPCH, TPCDS} if executor_requested else set())
    for filename in sorted(required):
        raw = (workspace / FIXTURES / filename).read_bytes()
        with patch.object(helpers, "FIXTURES_DIR", str(baseline / FIXTURES)):
            before = list(helpers.load_sql_fixture_pairs(filename))
        after = list(helpers.load_sql_fixture_pairs(filename))
        # The upstream loader intentionally drops a dangling statement. Replay
        # its complete input, but do not certify edited fixture truncation.
        if helpers._filter_comments(raw.decode("utf-8")).count(";") % 2:
            raise ValueError("odd number of fixture statements")
        loaded[filename] = (before, after, changed_units(before, after))

    plans = {}
    shared_sql = (not executor_requested or
                  {row[1] for row in loaded[TPCH][1]}.isdisjoint(row[1] for row in loaded[TPCDS][1]))
    for nodeid in proposed:
        if nodeid in EXECUTOR_FIXTURE:
            target, attribute = EXECUTOR_FIXTURE[nodeid]
            # The original class caches results by SQL without a suite key.
            if not shared_sql:
                continue
            before, after, units = loaded[target]
            if not read_only_queries(nodeid, (after[i] for i in units)):
                continue
            plans[nodeid] = {"kind": "executor", "files": {target: (after, units)}, "attribute": attribute,
                             "baseline_units": len(before)}
            continue
        files = {}
        for target, never_execute in optimizer[nodeid].items():
            if target not in fixtures:
                continue
            before, after, units = loaded[target]
            if not never_execute and any(helpers.string_to_bool(row[0].get("execute")) for row in before + after):
                # Fixture-driven DuckDB mutations can alter other assertions.
                break
            files[target] = after, units
        else:
            if files:
                plans[nodeid] = {"kind": "optimizer", "files": files}
    return plans


def replay(baseline, workspace, paths, targets):
    if not eligible_slices(paths, targets, baseline):
        return {"passed": [], "failed": [], "observations": {}}
    os.chdir(workspace)
    sys.path.insert(0, str(workspace))
    os.environ["SKIP_INTEGRATION"] = "0"
    import pytest
    import sqlglot
    from tests import helpers

    if not all(Path(module.__file__).absolute().is_relative_to(workspace) for module in (helpers, sqlglot)):
        raise RuntimeError("replay imported a module outside the patch tree")
    plans = prepare(baseline, workspace, paths, targets, helpers)
    if not plans:
        return {"passed": [], "failed": [], "observations": {}}

    class Recorder:
        def __init__(self):
            self.phases = {}
            self.errors = set()
            self.exercised = {}

        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_call(self, item):
            nodeid = item.nodeid
            plan = plans[nodeid]
            exercised = self.exercised[nodeid] = set()
            if plan["kind"] == "executor":
                target, (full, affected) = next(iter(plan["files"].items()))
                instance = item.instance
                if list(getattr(instance, plan["attribute"])) != full:
                    raise RuntimeError("patched shared setup did not load the expected full fixture")
                subset = [full[i] for i in affected]
                exercised.add(target)
                with substitute(instance, plan["attribute"], subset):
                    # The original _mp_execute still parses selected entries
                    # with execute=false and submits them over the real pool.
                    return (yield)

            from tests import test_optimizer

            original = test_optimizer.load_sql_fixture_pairs

            def load(filename):
                if filename not in plan["files"]:
                    return original(filename)
                full, affected = plan["files"][filename]
                if list(original(filename)) != full or filename in exercised:
                    raise RuntimeError("fixture has changed during replay or was read twice")
                exercised.add(filename)
                return (full[i] for i in affected)

            with patch.object(test_optimizer, "load_sql_fixture_pairs", load):
                # Preserve check_file's worker, AST integrity checks, original
                # assertions, plus assertions outside fixture loops.
                return (yield)

        def pytest_runtest_logreport(self, report):
            if getattr(report, "context", None) is not None:
                if report.failed:
                    self.errors.add(report.nodeid)
                return
            self.phases.setdefault(report.nodeid, {})[report.when] = report.outcome
            if report.failed:
                self.errors.add(report.nodeid)

        def pytest_collectreport(self, report):
            if report.failed:
                self.errors.update(nodeid for nodeid in plans if nodeid.partition("::")[0] == report.nodeid)

    recorder = Recorder()
    status = pytest.main([*sorted(plans), "-q", "--tb=no", "-p", "no:cacheprovider"], plugins=[recorder])
    if status not in (0, 1):
        return {"passed": [], "failed": sorted(recorder.errors), "observations": {}}
    passed = []
    observations = {}
    for nodeid, plan in sorted(plans.items()):
        phases = recorder.phases.get(nodeid, {})
        complete = recorder.exercised.get(nodeid) == set(plan["files"])
        if complete and nodeid not in recorder.errors and all(
                phases.get(phase) == "passed" for phase in ("setup", "call", "teardown")):
            passed.append(nodeid)
        observations[nodeid] = {
            "kind": plan["kind"], "phases": phases, "complete": complete,
            "fixture_units": {name: {"total": len(rows), "replayed": [i + 1 for i in affected]}
                              for name, (rows, affected) in plan["files"].items()},
        }
    return {"passed": passed, "failed": sorted(recorder.errors), "observations": observations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    receipt = args.receipt.absolute()
    outcome = replay(args.baseline.absolute(), args.workspace.absolute(), request["paths"], request["tests"])
    if "token" in request:
        outcome["token"] = request["token"]
    receipt.write_text(json.dumps(outcome, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
