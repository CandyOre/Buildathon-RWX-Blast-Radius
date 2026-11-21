"""Select tests from evidence measured on the unpatched SQLGlot revision."""

from __future__ import annotations

import ast
import gzip
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from equivalent_edits import normalize_module_tail
from patch_diff import UnsupportedPatch, parse_patch
from round3_guards import guard_proof
from round3_runtime import BASELINE, relative_source
from round3_semantics import control_masks, normalize_python
from scheduler import WORKER_COST_MILLISECONDS, test_milliseconds


REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)
CONTAINERS = (ast.Dict, ast.List, ast.Tuple, ast.Set)
EQUALITY_FOR = {ast.Is: ast.Eq, ast.IsNot: ast.NotEq}
BOUNDARY_FOR = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt}
SERIALIZATION_HOOKS = {"__reduce__", "__reduce_ex__", "__getstate__", "__setstate__",
                       "__getnewargs__", "__getnewargs_ex__"}


@dataclass
class Changes:
    functions: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.FunctionDef | ast.AsyncFunctionDef]] = (
        field(default_factory=list)
    )
    bindings: list[BindingChange] = field(default_factory=list)


@dataclass(frozen=True)
class BindingChange:
    name: str
    owner: str | None
    container: bool
    integer_value: bool
    requires_readers: bool
    eager_call: bool
    escape_key: str | None


@dataclass
class Selection:
    tests: list[dict]
    nearby: list[dict]
    evidence: dict
    applied: dict = field(default_factory=dict)


def same_syntax(old, new):
    if isinstance(old, ast.AST) and isinstance(new, ast.AST):
        return ast.dump(old) == ast.dump(new)
    if isinstance(old, list) and isinstance(new, list):
        return len(old) == len(new) and all(same_syntax(left, right) for left, right in zip(old, new))
    return type(old) is type(new) and old == new


def observed_equalities_only(old, new, sites):
    """Prove that the only AST change is replacing observed identity predicates."""
    if type(old) is not type(new):
        return False
    if isinstance(old, ast.Compare) and len(old.ops) == len(new.ops) == 1:
        if EQUALITY_FOR.get(type(old.ops[0])) is type(new.ops[0]):
            return ((old.lineno, old.col_offset) in sites and same_syntax(old.left, new.left)
                    and same_syntax(old.comparators, new.comparators))
    if isinstance(old, ast.AST):
        return all(observed_equalities_only(value, getattr(new, name), sites)
                   for name, value in ast.iter_fields(old))
    if isinstance(old, list):
        return len(old) == len(new) and all(observed_equalities_only(left, right, sites)
                                            for left, right in zip(old, new))
    return old == new


def single_integer_operator_site(old, new):
    """Return the old location if exactly one ordered operator changed."""
    pending = [(old, new)]
    site = None
    while pending:
        left, right = pending.pop()
        if type(left) is not type(right):
            return None
        if (isinstance(left, ast.Compare) and len(left.ops) == len(right.ops) == 1
                and BOUNDARY_FOR.get(type(left.ops[0])) is type(right.ops[0])):
            if site or not same_syntax(left.left, right.left) or not same_syntax(left.comparators, right.comparators):
                return None
            site = f"{left.lineno}:{left.col_offset}"
        elif isinstance(left, ast.AST):
            pending.extend((value, getattr(right, name)) for name, value in ast.iter_fields(left))
        elif isinstance(left, list):
            if len(left) != len(right):
                return None
            pending.extend(zip(left, right))
        elif left != right:
            return None
    return site


def safe_value_change(old, new, factories):
    """Permit constant changes and lambda bodies without changing eager evaluation."""
    if type(old) is not type(new):
        return False
    if isinstance(old, ast.Constant):
        return type(old.value) is type(new.value)
    if isinstance(old, ast.Lambda):
        return same_syntax(old.args, new.args)
    if isinstance(old, ast.Call) and isinstance(old.func, ast.Name) and old.func.id in factories:
        # This known factory only constructs a closure. Its body runs later.
        return (same_syntax(old.func, new.func) and safe_value_change(old.args, new.args, factories)
                and safe_value_change(old.keywords, new.keywords, factories))
    if isinstance(old, ast.keyword):
        return old.arg == new.arg and old.arg is not None and safe_value_change(old.value, new.value, factories)
    if isinstance(old, ast.Dict):
        # An importer can require a particular key even when the table itself is
        # a literal, for example a subclass that removes an inherited key.
        return same_syntax(old.keys, new.keys) and safe_value_change(old.values, new.values, factories)
    if isinstance(old, ast.Set):
        return same_syntax(old, new)
    if isinstance(old, (ast.List, ast.Tuple)):
        return all(safe_value_change(value, getattr(new, name), factories) for name, value in ast.iter_fields(old))
    if isinstance(old, ast.UnaryOp) and isinstance(old.op, (ast.UAdd, ast.USub)):
        return (type(old.op) is type(new.op) and isinstance(old.operand, ast.Constant)
                and type(old.operand.value) in (int, float, complex)
                and safe_value_change(old.operand, new.operand, factories))
    if isinstance(old, list):
        return len(old) == len(new) and all(safe_value_change(left, right, factories) for left, right in zip(old, new))
    return same_syntax(old, new)


def changed_binding(old, new, owner, factories, configuration_readers):
    if isinstance(old, ast.Assign) and isinstance(new, ast.Assign):
        if len(old.targets) == len(new.targets) == 1:
            left, right = old.targets[0], new.targets[0]
        else:
            return None
    elif isinstance(old, ast.AnnAssign) and isinstance(new, ast.AnnAssign):
        if not same_syntax(old.annotation, new.annotation):
            return None
        left, right = old.target, new.target
    else:
        return None
    if isinstance(left, ast.Name) and isinstance(right, ast.Name) and left.id == right.id:
        if left.id.isupper() or (owner and left.id == "arg_types"):
            integer_value = integer_literal(old.value) and integer_literal(new.value)
            ordinary_change = safe_value_change(old.value, new.value, factories)
            observed_integer = integer_value and f"{owner}:{left.id}" in configuration_readers
            if not ordinary_change and not observed_integer:
                return None
            return BindingChange(left.id, owner, isinstance(old.value, CONTAINERS),
                                 integer_value, not ordinary_change,
                                 eager_calls(old.value, factories) != eager_calls(new.value, factories),
                                 escape_key_change(old.value, new.value) if left.id == "NUMERIC_ESCAPES" else None)
    return None


def integer_literal(node):
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        node = node.operand
    return isinstance(node, ast.Constant) and type(node.value) is int


def escape_parameters(node):
    if not isinstance(node, ast.Tuple) or len(node.elts) != 4:
        return False
    if not all(isinstance(value, ast.Constant) and type(value.value) is int for value in node.elts):
        return False
    base, minimum, maximum, limit = (value.value for value in node.elts)
    return 2 <= base <= 36 and 1 <= minimum <= maximum <= 8 and 0 <= limit <= 0x10FFFF


def escape_key_change(old, new):
    if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict) or not same_syntax(old.keys, new.keys):
        return None
    if not all(isinstance(key, ast.Constant) and type(key.value) is str for key in old.keys):
        return None
    if len({key.value for key in old.keys}) != len(old.keys):
        return None
    differences = [(key.value, before, after) for key, before, after in zip(old.keys, old.values, new.values)
                   if not same_syntax(before, after)]
    if len(differences) == 1:
        key, before, after = differences[0]
        if escape_parameters(before) and escape_parameters(after):
            return key
    return None


def classify_statements(old_statements, new_statements, result, factories, configuration_readers, owner=None):
    if len(old_statements) != len(new_statements):
        return False
    for old, new in zip(old_statements, new_statements):
        if same_syntax(old, new):
            continue
        if isinstance(old, (*FUNCTION, ast.ClassDef)) and type(old) is type(new):
            if not all(same_syntax(value, getattr(new, name))
                       for name, value in ast.iter_fields(old) if name != "body"):
                return False
            if isinstance(old, ast.ClassDef):
                qualified_name = f"{owner}.{old.name}" if owner else old.name
                if not classify_statements(old.body, new.body, result, factories, configuration_readers,
                                           qualified_name):
                    return False
            else:
                result.functions.append((old, new))
        elif binding := changed_binding(old, new, owner, factories, configuration_readers):
            result.bindings.append(binding)
        else:
            return False
    return True


def boolean_operands(expression):
    if isinstance(expression, ast.BoolOp):
        return [operand for value in expression.values for operand in boolean_operands(value)]
    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        return boolean_operands(expression.operand)
    return [expression]


def local_expression_edit(old, new):
    """Recognize value and operator changes without changing bindings or call sites."""
    if same_syntax(old, new):
        return True
    if isinstance(old, ast.expr) and isinstance(new, ast.expr):
        operands = boolean_operands(old)
        amended_operands = boolean_operands(new)
        if operands != [old] or amended_operands != [new]:
            return (len(operands) == len(amended_operands)
                    and all(local_expression_edit(left, right) for left, right in zip(operands, amended_operands)))
    if type(old) is not type(new):
        return False
    if isinstance(old, ast.Constant):
        return type(old.value) is type(new.value)
    if isinstance(old, (ast.Compare, ast.BinOp)):
        operator_fields = {"ops", "op"}
        return all(
            (len(value) == len(getattr(new, name)) if isinstance(value, list) else True)
            if name in operator_fields else local_expression_edit(value, getattr(new, name))
            for name, value in ast.iter_fields(old)
        )
    if isinstance(old, ast.Call) and not same_syntax(old.func, new.func):
        return False
    if isinstance(old, (ast.Call, ast.IfExp, ast.UnaryOp, ast.NamedExpr, ast.Subscript, ast.keyword)):
        return all(local_expression_edit(value, getattr(new, name)) for name, value in ast.iter_fields(old))
    if isinstance(old, list):
        return len(old) == len(new) and all(local_expression_edit(left, right) for left, right in zip(old, new))
    return False


def statement_regions(before, after):
    """Find old statement entries that a test must reach before a local edit matters."""
    if len(before) != len(after):
        return None
    regions = []
    for old, new in zip(before, after):
        if same_syntax(old, new):
            continue
        if type(old) is not type(new) or isinstance(old, (*FUNCTION, ast.ClassDef)):
            return None
        if isinstance(old, (ast.If, ast.While)):
            local_field = "test"
        elif isinstance(old, (ast.Assign, ast.AnnAssign, ast.Return)):
            local_field = "value"
        else:
            local_field = None
        for name, previous in ast.iter_fields(old):
            updated = getattr(new, name)
            if same_syntax(previous, updated):
                continue
            if name == local_field and local_expression_edit(previous, updated):
                regions.append(range(old.lineno, previous.end_lineno + 1))
            elif name in {"body", "orelse", "finalbody"} and isinstance(previous, list):
                nested = statement_regions(previous, updated)
                if nested is None:
                    return None
                regions.extend(nested)
            else:
                return None
    return regions


def eager_calls(node, factories):
    if isinstance(node, ast.Lambda):
        return eager_calls(node.args, factories)
    if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in factories):
        return [ast.dump(node)]
    return [call for child in ast.iter_child_nodes(node) for call in eager_calls(child, factories)] if node else []


def is_eager(function, lines):
    return min([function.lineno, *(decorator.lineno for decorator in function.decorator_list)]) in lines


def deferred_functions(old, new, eager_lines):
    """Allow local edits in uncalled closures while preserving their creation."""
    if len(old.body) != len(new.body):
        return None
    changes = []
    for before, after in zip(old.body, new.body):
        if same_syntax(before, after):
            continue
        if not isinstance(before, FUNCTION) or type(before) is not type(after):
            return None
        if not all(same_syntax(value, getattr(after, name)) for name, value in ast.iter_fields(before)
                   if name != "body"):
            return None
        if is_eager(before, eager_lines) or not statement_regions(before.body, after.body):
            return None
        changes.append((before, after))
    return changes


class ImpactIndex:
    def __init__(self, path: Path, tests: list[dict], *, module_pass=True, integer_comparisons=True,
                 numeric_comparisons=True, numeric_escapes=True, semantics=True, control=True, baseline=BASELINE):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            self.index = json.load(stream)
        if self.index["version"] != 1 or self.index["revision"] != REVISION:
            raise ValueError("unsupported test impact index")
        test_positions = {nodeid: position for position, nodeid in enumerate(self.index["tests"])}
        self.known = [(1 << test_positions[test["id"]], test, test_milliseconds(test))
                      for test in tests if test["id"] in test_positions]
        self.unknown = [test for test in tests if test["id"] not in test_positions]
        self.uncovered = int(self.index["uncovered"], 16)
        self.position_observers = self.index.get("position_observers")
        self.module_pass = module_pass
        self.integer_sites = self.index.get("integer_comparison_sites", {}) if integer_comparisons else {}
        self.numeric_sites = (self.index.get("numeric_comparison_sites", {})
                              if integer_comparisons and numeric_comparisons else {})
        self.numeric_escapes = numeric_escapes
        self.semantics = semantics
        self.control = control
        self.baseline = baseline
        self.test_files = {}
        for bit, test, _ in self.known:
            self.test_files[test["file"]] = self.test_files.get(test["file"], 0) | bit
        self.cheap = 0
        for bit, _, milliseconds in self.known:
            if milliseconds <= WORKER_COST_MILLISECONDS // 20:
                self.cheap |= bit
        self.equality_sites = {path: {tuple(site) for site in sites}
                               for path, sites in self.index.get("identity_equality_sites", {}).items()}
        self.trees = {}
        self.line_masks = {}

    def file_bits(self, path, evidence, functions, *, broad=False):
        if path not in self.line_masks:
            self.line_masks[path] = {int(line): int(mask, 16) for line, mask in evidence["lines"].items()}
        covered_lines = self.line_masks[path]
        background = set(evidence["background"])
        bits, unscoped, narrowed = 0, False, False
        for old, new in functions:
            body = range(old.body[0].lineno, old.end_lineno + 1)
            counterpart = int(evidence["pickle_counterparts"].get(old.name, "0"), 16)
            witnesses = counterpart
            for line in body:
                witnesses |= covered_lines.get(line, 0)
            if not witnesses:
                witnesses = int(evidence["file"], 16)
            elif not broad and not counterpart and background.isdisjoint(body):
                regions = statement_regions(old.body, new.body)
                if regions:
                    anchored = 0
                    for region in regions:
                        entered = 0
                        for line in region:
                            entered |= covered_lines.get(line, 0)
                        if not entered:
                            break
                        anchored |= entered
                    else:
                        anchored |= witnesses & self.cheap
                        narrowed |= witnesses != anchored
                        witnesses = anchored
            bits |= witnesses
            unscoped |= not background.isdisjoint(body)
        return bits, unscoped, narrowed

    def source_impact(self, path, patched, evidence, changed_files):
        if path not in self.trees:
            self.trees[path] = ast.parse(evidence["source"], filename=path, type_comments=True)
        old = self.trees[path]
        new = ast.parse(patched.encode("utf-8"), filename=path, type_comments=True)
        readers = int(evidence["readers"], 16)
        normalized, rejected = normalize_module_tail(old, new, patched, path) if self.module_pass else (None, None)
        reason_suffix = ""
        if normalized:
            effective, positions_changed = normalized
            if positions_changed and self.position_observers is None:
                rejected = "module positions changed without observer data"
            else:
                new = effective
                reason_suffix = "+compiled-module-pass"
                if positions_changed:
                    readers |= int(self.position_observers, 16) | self.uncovered
        if self.semantics and not normalized:
            proof = normalize_python(evidence["source"], patched, path, old, new)
            if proof and not (proof[1] == "compiled-equivalent-local-names" and len(changed_files) > 1):
                new, reason, keep_positions = proof
                reason_suffix = f"+{reason}"
                if keep_positions:
                    if self.position_observers is None:
                        raise UnsupportedPatch("source-position witnesses unavailable")
                    readers |= int(self.position_observers, 16) | self.uncovered
        if same_syntax(old, new):
            return readers, 0, "unchanged-python-ast" + reason_suffix
        if self.control and len(changed_files) == 1 and guard_proof(path, old, new, self.index):
            return readers | int(self.position_observers or "0", 16) | self.uncovered, 0, "inactive-entry-guard"

        compile(new, path, "exec")
        sites = self.equality_sites.get(path)
        changes = Changes()
        factories = {name for name, origin in evidence["deferred_factories"].items() if origin not in changed_files}
        configuration_readers = evidence.get("configuration_readers", {}) if len(changed_files) == 1 else {}
        if (not same_syntax(old.type_ignores, new.type_ignores)
                or not classify_statements(old.body, new.body, changes, factories, configuration_readers)):
            detail = f" ({rejected})" if rejected else ""
            raise UnsupportedPatch(f"{path}: Python declarations or imports changed shape{detail}")

        observed_file = int(evidence["file"], 16)
        selected = readers | self.uncovered
        eager_lines = evidence.get("eager_lines", ())
        if any(binding.eager_call for binding in changes.bindings):
            raise UnsupportedPatch(f"{path}: import-time registration call changed")
        functions = []
        for old_function, new_function in changes.functions:
            if old_function.name in SERIALIZATION_HOOKS:
                raise UnsupportedPatch(f"{path}: serialization protocol {old_function.name} changed")
            if not is_eager(old_function, eager_lines):
                functions.append((old_function, new_function))
                continue
            deferred = deferred_functions(old_function, new_function, eager_lines)
            if not deferred:
                raise UnsupportedPatch(f"{path}: import-time function {old_function.name} changed")
            functions.extend(deferred)
            selected |= int(self.position_observers or "0", 16)
            reason_suffix += "+deferred-closure"
        changes.functions = functions
        if len(changed_files) == 1 and sites and observed_equalities_only(old, new, sites):
            return selected, 0, "observed-identity-equality-agreement" + reason_suffix
        if self.numeric_escapes and len(changed_files) == 1 and len(changes.bindings) == 1 and not changes.functions:
            binding = changes.bindings[0]
            tables = evidence.get("numeric_escape_readers", {})
            if binding.escape_key in tables.get(binding.owner, {}):
                witnesses = int(tables[binding.owner][binding.escape_key], 16)
                return (selected | witnesses | int(self.position_observers or "0", 16), 0,
                        "escape-key-readers" + reason_suffix)
        if configuration_readers and len(changes.bindings) == 1 and not changes.functions:
            binding = changes.bindings[0]
            if binding.owner and binding.integer_value:
                key = f"{binding.owner}:{binding.name}"
                configuration = int(configuration_readers.get(key, "0"), 16)
                if configuration:
                    return selected | configuration, observed_file, "configuration-readers" + reason_suffix
        if any(binding.requires_readers for binding in changes.bindings):
            raise UnsupportedPatch("configuration value edit requires an isolated patch")

        reason = "function-coverage"
        if changes.functions:
            multi_source = self.semantics and sum(file.startswith("sqlglot/") for file in changed_files) > 1
            functions, unscoped, narrowed = self.file_bits(path, evidence, changes.functions, broad=multi_source)
            if not functions:
                raise UnsupportedPatch("changed functions lack baseline evidence")
            selected |= functions
            if unscoped or multi_source:
                selected |= int(evidence["reverse_importers"], 16)
                reason = "function-coverage-and-importers"
            elif narrowed:
                reason = "statement-coverage"

        if changes.bindings:
            selected |= observed_file | int(evidence["direct_importers"], 16)
            dialect = evidence["dialect"]
            dialect_tests = int(self.index["dialects"].get(dialect, "0"), 16)
            selected |= dialect_tests
            for binding in changes.bindings:
                if binding.owner and binding.container and dialect_tests:
                    # A class registry is normally reached through its concrete
                    # dialect. The same attribute on other dialects is unrelated.
                    continue
                reference = binding.owner.rsplit(".", 1)[-1] if binding.name == "arg_types" else binding.name
                selected |= int(self.index["bindings"].get(reference, "0"), 16)
            if not (selected & ~self.uncovered):
                raise UnsupportedPatch("changed declarations lack baseline evidence")
            reason = "declaration-coverage"

        # Keep the wider file envelope when its additional serial load costs
        # no more than starting one extra worker.
        omitted_file_tests = observed_file & ~selected
        extra_milliseconds = sum(duration for bit, _, duration in self.known if bit & omitted_file_tests)
        if omitted_file_tests and extra_milliseconds <= WORKER_COST_MILLISECONDS:
            selected |= observed_file
            reason += "+cheap-file-tests"
        comparisons = self.integer_sites.get(path, {})
        numeric = self.numeric_sites.get(path, {})
        if ((comparisons or numeric) and self.position_observers is not None and len(changed_files) == 1 and not reason_suffix
                and len(changes.functions) == 1 and not changes.bindings):
            site = single_integer_operator_site(*changes.functions[0])
            exact = comparisons.get(site)
            extended = numeric.get(site)
            if observations := extended or exact:
                boundary, uncertain, removable = (int(mask, 16) for mask in observations[:3])
                required = readers | int(self.position_observers, 16) | self.uncovered | boundary | uncertain
                selected = (selected & ~removable) | required
                observed_file &= ~removable
                if len(observations) == 4:
                    detail = f":{observations[3]}"
                else:
                    detail = "" if removable else ":no-confirmed-nonboundary"
                kind = "numeric" if extended and extended != exact else "integer"
                reason += f"+{kind}-boundary{detail}"
        if self.control and len(changed_files) == 1 and len(changes.functions) == 1 and not changes.bindings:
            proof = control_masks(path, *changes.functions[0], self.index)
            if proof:
                witnesses, removable, control_reason = proof
                required = readers | int(self.position_observers or "0", 16) | self.uncovered | witnesses
                selected = (selected & ~removable) | required
                observed_file &= ~removable
                reason += f"+{control_reason}"
        return selected, observed_file, reason + reason_suffix

    def applied_tree(self, changes):
        applied = {}
        for change in changes:
            file = self.index["files"].get(change.path)
            if file:
                before = file["source"]
            elif self.baseline.is_dir():
                before = relative_source(self.baseline, change.path).read_bytes().decode("utf-8")
            elif change.path in self.index["fixtures"]:
                continue
            else:
                raise UnsupportedPatch("file has no baseline impact data")
            applied[change.path] = before, change.apply(before)
        return applied

    def selection(self, patch: Path, tests: list[dict]) -> Selection:
        selected, nearby = 0, 0
        evidence_used = Counter()
        applied = {}
        try:
            changes = parse_patch(patch.read_text(encoding="utf-8"))
            changed_files = {change.path for change in changes}
            applied = self.applied_tree(changes)
            for change in changes:
                if change.path in self.index["fixtures"]:
                    bits, related, reason = int(self.index["fixtures"][change.path], 16), 0, "fixture-owners"
                elif change.path in self.index["files"]:
                    bits, related, reason = self.source_impact(change.path, applied[change.path][1],
                                                              self.index["files"][change.path], changed_files)
                elif self.semantics and change.path in self.test_files:
                    before, after = applied[change.path]
                    old = ast.parse(before, filename=change.path, type_comments=True)
                    new = ast.parse(after, filename=change.path, type_comments=True)
                    proof = normalize_python(before, after, change.path, old, new)
                    if not proof or not same_syntax(old, proof[0]):
                        raise UnsupportedPatch(f"{change.path}: test execution or shared state changed")
                    bits, related, reason = self.test_files[change.path], 0, "test-source-observers"
                    bits |= int(self.position_observers or "0", 16)
                else:
                    raise UnsupportedPatch("file has no baseline impact data")
                selected |= bits
                nearby |= related
                evidence_used[reason] += 1
        except (OSError, SyntaxError, ValueError, RecursionError) as error:
            return Selection(tests, [], {"full-suite": str(error)}, applied)
        return Selection(
            [test for bit, test, _ in self.known if bit & selected] + self.unknown,
            [test for bit, test, _ in self.known if bit & nearby and not bit & selected],
            dict(evidence_used),
            applied,
        )

    def select(self, patch: Path, tests: list[dict]):
        """Return the required candidates for analysis without scheduling."""
        selection = self.selection(patch, tests)
        return selection.tests, selection.evidence
