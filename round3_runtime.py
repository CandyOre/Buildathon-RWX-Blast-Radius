"""Isolate patch overlays and supervise bounded verification subprocesses."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from patch_diff import UnsupportedPatch

try:
    import resource
except ImportError:
    resource = None


BASELINE = Path("/opt/sqlglot")
TOOLS = Path(__file__).resolve().parent
RUNTIME_REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def patch_path(path):
    parts = PurePosixPath(path).parts
    if (not parts or parts[0] not in {"sqlglot", "tests", "sqlglotc"}
            or ".." in parts or "\\" in path or path.startswith("/") or "/".join(parts) != path):
        raise UnsupportedPatch("patch path is outside the pinned checkout")
    return parts


def relative_source(baseline, path):
    source = baseline.joinpath(*patch_path(path))
    if not source.is_file() or source.is_symlink():
        raise UnsupportedPatch("patch source is unavailable in the pinned checkout")
    return source


def populate_overlay(baseline, workspace, changes):
    editing = {}
    for path, (before, after) in changes.items():
        source = relative_source(baseline, path)
        if source.read_bytes() != before.encode("utf-8"):
            raise UnsupportedPatch(f"{path}: patch baseline differs from the bundled source")
        parts = patch_path(path)
        branches = editing
        for part in parts[:-1]:
            branches = branches.setdefault(part, {})
        branches[parts[-1]] = after

    def copy_branches(original, target, branches):
        for child in original.iterdir():
            overlay = target / child.name
            if child.name not in branches:
                overlay.symlink_to(child, target_is_directory=child.is_dir())
            elif isinstance(branch := branches[child.name], dict):
                overlay.mkdir()
                copy_branches(child, overlay, branch)
            else:
                overlay.write_bytes(branch.encode("utf-8"))

    copy_branches(baseline, workspace, editing)


@dataclass
class BatchBudget:
    limit_seconds: float = 24
    started: float = field(default_factory=time.monotonic)
    snapshots: dict = field(default_factory=dict)

    def allowed(self, per_call):
        remaining = self.limit_seconds - (time.monotonic() - self.started)
        return max(0.0, min(remaining, per_call))


@dataclass
class Receipt:
    passed: frozenset = frozenset()
    failed: frozenset = frozenset()
    detail: dict = field(default_factory=dict)
    status: str = "unavailable"
    elapsed: float = 0
    cpu: float = 0


class PatchRuntime:
    def __init__(self, baseline, workspace, scratch, budget):
        self.baseline = baseline
        self.workspace = workspace
        self.scratch = scratch
        self.budget = budget
        self.baseline_snapshots = budget.snapshots

    @staticmethod
    def children_cpu():
        if resource is None:
            return 0
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return usage.ru_utime + usage.ru_stime

    def process(self, script, arguments, receipt_path, *, timeout, cwd=None):
        allowed = self.budget.allowed(timeout)
        if allowed < 0.15:
            return None, "budget", 0
        cwd = cwd or self.workspace
        environment = {
            **os.environ, "PYTHONPATH": str(cwd), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "",
            "SKIP_INTEGRATION": "0", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1", "TMPDIR": str(self.scratch),
        }
        start = time.monotonic()
        try:
            with subprocess.Popen([sys.executable, str(TOOLS / script), *map(str, arguments)],
                                  cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True) as child:
                try:
                    status = child.wait(timeout=allowed)
                except subprocess.TimeoutExpired:
                    stop_process_group(child)
                    child.wait()
                    return None, "timeout", time.monotonic() - start
                finally:
                    if os.name == "posix":
                        stop_process_group(child)
            elapsed = time.monotonic() - start
            if status or receipt_path.stat().st_size > 2_000_000:
                return None, "incomplete", elapsed
            data = json.loads(receipt_path.read_bytes())
            return (data, "complete", elapsed) if isinstance(data, dict) else (None, "incomplete", elapsed)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None, "unavailable", time.monotonic() - start

    def snapshot(self, modules, *, timeout=2.0):
        key = tuple(sorted(set(modules)))
        snapshots = []
        for directory in (self.baseline, self.workspace):
            if directory == self.baseline and key in self.baseline_snapshots:
                snapshots.append(self.baseline_snapshots[key])
                continue
            path = self.scratch / f"snapshot-{uuid.uuid4().hex}.json"
            result, _, _ = self.process("round3_imports.py", ("snapshot", directory, path, *key), path,
                                        timeout=timeout, cwd=directory)
            if result is None:
                return None
            snapshots.append(result)
            if directory == self.baseline:
                self.baseline_snapshots[key] = result
        return tuple(snapshots)

    def collect(self, *, timeout=3.0):
        path = self.scratch / f"collection-{uuid.uuid4().hex}.json"
        data, _, _ = self.process("round3_imports.py", ("collect", self.workspace, path), path, timeout=timeout)
        return data

    def verify(self, kind, nodeids, paths, *, timeout):
        token = uuid.uuid4().hex
        request = self.scratch / f"request-{token}.json"
        response = self.scratch / f"result-{token}.json"
        request.write_text(json.dumps({"token": token, "paths": paths, "tests": sorted(set(nodeids))}),
                           encoding="utf-8")
        if kind == "slice":
            script = "round3_slices.py"
            arguments = ("--baseline", self.baseline, "--workspace", self.workspace, "--request", request,
                         "--receipt", response)
        else:
            script = "round3_verify.py"
            arguments = (self.workspace, request, response)
        cpu = self.children_cpu()
        data, status, elapsed = self.process(script, arguments, response, timeout=timeout)
        if data is None:
            return Receipt(status=status, elapsed=elapsed, cpu=self.children_cpu() - cpu)
        receipt = validate_receipt(data, nodeids, token)
        receipt.elapsed = elapsed
        receipt.cpu = self.children_cpu() - cpu
        return receipt


def validate_receipt(data, nodeids, token=None):
    if token is not None and token != data.get("token"):
        return Receipt(status="incomplete")
    keys = ("passed", "failed")
    if any(not isinstance(data.get(key), list) or not all(isinstance(item, str) for item in data[key])
           for key in keys):
        return Receipt(status="incomplete")
    passed, failed = (frozenset(data[key]) for key in keys)
    requested = frozenset(nodeids)
    if passed & failed or not (passed | failed) <= requested:
        return Receipt(status="incomplete")
    detail = data.get("observations", {})
    if not isinstance(detail, dict):
        return Receipt(status="incomplete")
    return Receipt(passed, failed, detail, "complete" if requested == passed | failed else "partial")


def stop_process_group(child):
    try:
        if os.name == "posix":
            os.killpg(child.pid, signal.SIGKILL)
        else:
            child.kill()
    except ProcessLookupError:
        pass


@contextmanager
def patch_runtime(baseline, changes, budget):
    baseline = baseline.resolve()
    marker = baseline / ".revision"
    if not marker.is_file() or marker.read_text(encoding="ascii").strip() != RUNTIME_REVISION:
        raise UnsupportedPatch("bundled SQLGlot revision is unavailable")
    with tempfile.TemporaryDirectory(prefix="blast-radius-") as directory:
        root = Path(directory)
        workspace = root / "checkout"
        scratch = root / "scratch"
        workspace.mkdir()
        scratch.mkdir()
        populate_overlay(baseline, workspace, changes)
        yield PatchRuntime(baseline, workspace, scratch, budget)
