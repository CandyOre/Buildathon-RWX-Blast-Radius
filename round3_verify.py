"""Run unchanged pytest assertions in a patch overlay and emit complete receipts."""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path


class Results:
    def __init__(self, requested):
        self.requested = requested
        self.discovered = set()
        self.phases = defaultdict(dict)
        self.failed_subtests = set()
        self.collection_failures = set()

    def pytest_collection_modifyitems(self, config, items):
        self.discovered.update(item.nodeid for item in items if item.nodeid in self.requested)
        dropped = [item for item in items if item.nodeid not in self.requested]
        items[:] = [item for item in items if item.nodeid in self.requested]
        config.hook.pytest_deselected(items=dropped)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_failures.add(report.nodeid)

    def pytest_runtest_logreport(self, report):
        if getattr(report, "context", None) is not None:
            if report.failed:
                self.failed_subtests.add(report.nodeid)
            return
        self.phases[report.nodeid][report.when] = report.outcome

    def receipt(self, token, status):
        passed, failed = [], []
        for node in sorted(self.requested):
            phases = self.phases[node]
            file = node.split("::", 1)[0]
            if (node in self.failed_subtests or file in self.collection_failures
                    or "failed" in phases.values()):
                failed.append(node)
            elif (status in (0, 1) and node in self.discovered
                  and all(phases.get(phase) == "passed" for phase in ("setup", "call", "teardown"))):
                passed.append(node)
        return {"token": token, "passed": passed, "failed": failed,
                "observations": {"collection_failure_files": sorted(self.collection_failures),
                                 "discovered": len(self.discovered), "pytest_status": int(status)}}


def main():
    workspace, request_path, receipt_path = (Path(value).absolute() for value in sys.argv[1:])
    request = json.loads(request_path.read_bytes())
    requested = set(request["tests"])
    os.chdir(workspace)
    sys.path.insert(0, str(workspace))
    import pytest
    import sqlglot

    if not Path(sqlglot.__file__).is_relative_to(workspace / "sqlglot"):
        raise RuntimeError("pytest did not load SQLGlot from the patch overlay")
    result = Results(requested)
    status = pytest.main(["tests", "-q", "--tb=no", "-p", "no:cacheprovider", "-p", "pytest_timeout",
                          "--timeout=3", "--timeout-method=signal", "--continue-on-collection-errors"],
                         plugins=[result])
    receipt = result.receipt(request["token"], status)
    if status not in (0, 1) or result.collection_failures:
        receipt["passed"] = []
    pending = receipt_path.with_suffix(".tmp")
    pending.write_text(json.dumps(receipt), encoding="utf-8")
    pending.replace(receipt_path)


if __name__ == "__main__":
    main()
