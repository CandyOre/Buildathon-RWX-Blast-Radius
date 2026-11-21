"""Compare cold parser-registry state and select its observed consumers."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import pkgutil
import sys
import warnings
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from types import CodeType, FunctionType, MethodType, ModuleType


SCHEMA = 1
PINNED = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


@dataclass(frozen=True)
class RegistryEdit:
    path: str
    module: str
    owner: str
    keys: frozenset[str]


def named_bindings(tree):
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign):
                targets = stmt.targets
            elif isinstance(stmt, ast.AnnAssign):
                targets = [stmt.target]
            else:
                continue
            if len(targets) == 1 and isinstance(targets[0], ast.Name) and targets[0].id == "FUNCTIONS":
                yield cls.name, stmt


def pure_factories(tree):
    """Recognize local functions whose eager evaluation only constructs a closure."""
    if not any(isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__"
               and any(alias.name == "annotations" for alias in stmt.names) for stmt in tree.body):
        return set()
    names = set()
    for outer in tree.body:
        if not isinstance(outer, ast.FunctionDef) or outer.decorator_list:
            continue
        body = outer.body[1:] if ast.get_docstring(outer) else outer.body
        if len(body) != 2:
            continue
        inner, returned = body
        if (isinstance(inner, ast.FunctionDef) and not inner.decorator_list and not inner.type_params
                and not inner.args.defaults and not any(inner.args.kw_defaults)
                and isinstance(returned, ast.Return) and isinstance(returned.value, ast.Name)
                and returned.value.id == inner.name):
            names.add(outer.name)
    return names


def plain_argument(value):
    if isinstance(value, ast.Constant):
        return type(value.value) in (str, int, bool, float, type(None))
    return isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "exp"


def pure_entry(value, factories):
    if isinstance(value, ast.Lambda):
        return not value.args.defaults and not any(value.args.kw_defaults)
    if isinstance(value, ast.Name):
        return True
    if isinstance(value, ast.Attribute):
        root = value.value
        while isinstance(root, ast.Attribute):
            root = root.value
        return isinstance(root, ast.Name) and root.id == "exp"
    return (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in factories
            and all(plain_argument(arg) for arg in value.args)
            and all(kw.arg is not None and plain_argument(kw.value) for kw in value.keywords))


def literal_table(node):
    if not isinstance(node, ast.Dict):
        return None
    spread = []
    entries = {}
    for key, value in zip(node.keys, node.values):
        if key is None:
            # Copies must precede named entries to keep precedence unambiguous.
            if entries:
                return None
            spread.append(ast.dump(value))
        elif not isinstance(key, ast.Constant) or type(key.value) is not str or key.value in entries:
            return None
        else:
            entries[key.value] = value
    return spread, entries


def registry_edit(applied):
    if len(applied) != 1:
        return None
    path, (before, after) = next(iter(applied.items()))
    parts = PurePosixPath(path).parts
    if len(parts) != 3 or parts[:2] != ("sqlglot", "parsers") or not path.endswith(".py"):
        return None
    left, right = [ast.parse(source, filename=path, type_comments=True) for source in (before, after)]
    left_bindings, right_bindings = map(lambda tree: dict(named_bindings(tree)), (left, right))
    if set(left_bindings) != set(right_bindings):
        return None
    changed = [name for name, value in left_bindings.items() if ast.dump(value) != ast.dump(right_bindings[name])]
    if len(changed) != 1:
        return None
    owner = changed[0]
    a, b = left_bindings[owner], right_bindings[owner]
    old, new = literal_table(a.value), literal_table(b.value)
    if old is None or new is None or old[0] != new[0]:
        return None
    # Substituting both values removes exactly the one candidate AST difference.
    a.value = b.value = ast.Constant(None)
    if ast.dump(left) != ast.dump(right):
        return None
    old_keys, new_keys = old[1], new[1]
    common = old_keys.keys() & new_keys.keys()
    if [k for k in old_keys if k in common] != [k for k in new_keys if k in common]:
        return None
    keys = old_keys.keys() ^ new_keys.keys() | {k for k in common if ast.dump(old_keys[k]) != ast.dump(new_keys[k])}
    factories = pure_factories(left)
    if not keys or not all(pure_entry(table[k], factories) for k in keys for table in (old_keys, new_keys) if k in table):
        return None
    module = path[:-3].replace("/", ".")
    return RegistryEdit(path, module, f"{module}:{owner}", frozenset(keys))


class StableState:
    """Encode supported values without executing patched comparison or representation hooks."""

    def __init__(self):
        self.cache = {}
        self.active = set()

    def digest(self, value):
        key = id(value)
        cached = self.cache.get(key)
        if cached is not None and cached[0] is value:
            return cached[1]
        if key in self.active:
            return None
        self.active.add(key)
        try:
            encoded = self.encode(value)
            digest = None if encoded is None else hashlib.sha256(
                json.dumps(encoded, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
            self.cache[key] = value, digest
            return digest
        finally:
            self.active.remove(key)

    def sequence(self, values):
        items = [self.digest(value) for value in values]
        return None if None in items else items

    def encode(self, value):
        kind = type(value)
        if kind in (type(None), bool, int, str, float):
            return [kind.__name__, value]
        if kind is bytes:
            return ["bytes", value.hex()]
        if isinstance(value, Enum):
            return ["enum", kind.__module__, kind.__qualname__, object.__getattribute__(value, "_name_")]
        if kind in (tuple, list, set, frozenset):
            items = self.sequence(value)
            if items is not None:
                return [kind.__name__, sorted(items) if kind in (set, frozenset) else items]
        if kind is dict:
            entries = self.sequence(value.items())
            if entries is not None:
                return ["dict", entries]
        if kind is CodeType:
            constants = self.sequence(value.co_consts)
            if constants is not None:
                return ["code", value.co_code.hex(), value.co_exceptiontable.hex(), constants, value.co_names,
                        value.co_varnames, value.co_freevars, value.co_cellvars, value.co_flags,
                        value.co_argcount, value.co_posonlyargcount, value.co_kwonlyargcount]
        if kind is FunctionType:
            captures = tuple(cell.cell_contents for cell in value.__closure__ or ())
            values = self.sequence((value.__code__, value.__defaults__, value.__kwdefaults__, captures))
            if values is not None:
                return ["function", value.__module__, value.__qualname__, values]
        if kind is MethodType:
            values = self.sequence((value.__func__, value.__self__))
            if values is not None:
                return ["method", values]
        if isinstance(value, type):
            return ["class", type.__getattribute__(value, "__module__"), type.__getattribute__(value, "__qualname__")]
        if kind is ModuleType:
            return ["module", value.__name__]
        return None


def under_root(root, module):
    """Use the load path rather than its resolved symlink target for overlays."""
    return Path(os.path.abspath(module.__file__)).is_relative_to(root)


def loaded_parser_classes(base):
    found = set()
    pending = [base]
    while pending:
        cls = pending.pop()
        if cls not in found:
            found.add(cls)
            pending.extend(cls.__subclasses__())
    return sorted(found, key=lambda cls: f"{cls.__module__}:{cls.__qualname__}")


def snapshot(root, requested):
    root = Path(root).resolve()
    sys.path.insert(0, str(root))
    os.chdir(root)
    imports = {}
    derived = {}
    registries = {}
    aliases = defaultdict(list)
    encoder = StableState()
    try:
        from sqlglot import parser, exp, tokens

        if not under_root(root, sys.modules["sqlglot"]):
            raise RuntimeError("loaded SQLGlot from an unrelated checkout")
        # Loading dialect modules also exercises inherited registration and metaclass creation.
        from sqlglot import dialects, parsers

        packages = (dialects, parsers)
        discoverable = {entry.name for package in packages
                        for entry in pkgutil.iter_modules(package.__path__, package.__name__ + ".")}
        for name in sorted(discoverable | set(requested)):
            try:
                module = importlib.import_module(name)
                if not under_root(root, module):
                    raise RuntimeError("loaded a module from an unrelated checkout")
                imports[name] = None
            except Exception as error:
                imports[name] = type(error).__name__
        for cls in loaded_parser_classes(parser.Parser):
            name = f"{cls.__module__}:{cls.__qualname__}"
            namespace = type.__getattribute__(cls, "__dict__")
            if "FUNCTIONS" in namespace and type(table := namespace["FUNCTIONS"]) is dict:
                aliases[id(table)].append(name)
                if not all(type(key) is str for key in table):
                    raise TypeError("non-string parser registry key")
                entries = {key: encoder.digest(value) for key, value in table.items()}
                registries[name] = {"keys": list(table), "entries": entries}
            other = ("FUNCTION_PARSERS", "NO_PAREN_FUNCTION_PARSERS", "NO_PAREN_FUNCTIONS", "PROPERTY_PARSERS")
            derived[name] = {attr: encoder.digest(namespace[attr]) for attr in other if attr in namespace}
        for name, cls in vars(exp).items():
            if isinstance(cls, type) and issubclass(cls, exp.Expr):
                namespace = type.__getattribute__(cls, "__dict__")
                fields = ("arg_types", "required_args")
                if any(attr in namespace for attr in fields):
                    derived[f"expression:{name}"] = {attr: encoder.digest(namespace[attr])
                                                       for attr in fields if attr in namespace}
        if any(name == "sqlglot.tokens" or name.startswith("sqlglot.dialects.") for name in requested):
            for cls in loaded_parser_classes(tokens.Tokenizer):
                namespace = type.__getattribute__(cls, "__dict__")
                fields = ("KEYWORDS", "_KEYWORD_TRIE", "NUMERIC_ESCAPES")
                if any(attr in namespace for attr in fields):
                    derived[f"tokenizer:{cls.__module__}:{cls.__qualname__}"] = {
                        attr: encoder.digest(namespace[attr]) for attr in fields if attr in namespace
                    }
        # Keep supported directly visible globals and class attributes alongside derived registries.
        for name in requested:
            if imports.get(name) is not None:
                continue
            namespace = vars(sys.modules[name])
            derived[f"module:{name}"] = {key: encoder.digest(value) for key, value in namespace.items()
                                           if not key.startswith("__")}
            for cls in loaded_parser_classes(parser.Parser):
                if cls.__module__ == name:
                    state = type.__getattribute__(cls, "__dict__")
                    derived[f"class:{name}:{cls.__qualname__}"] = {
                        key: encoder.digest(value) for key, value in state.items()
                        if not key.startswith("__") and key != "FUNCTIONS"
                    }
    except Exception as error:
        imports["$snapshot"] = type(error).__name__
    return {"version": SCHEMA, "imports": imports, "registries": registries, "derived": derived,
            "aliases": sorted(sorted(names) for names in aliases.values() if len(names) > 1)}


def changed_registry_keys(before, after, edit):
    fields = {"imports": dict, "derived": dict, "registries": dict, "aliases": list}
    if any(not isinstance(report, dict) or report.get("version") != SCHEMA
           or any(not isinstance(report.get(name), kind) for name, kind in fields.items())
           for report in (before, after)):
        return None
    imports = before.get("imports", {})
    if (edit.module not in imports or imports != after.get("imports") or any(imports.values())
            or before["derived"] != after["derived"] or before["aliases"] != after["aliases"]
            or before.get("warnings") != after.get("warnings")):
        return None
    old, new = before["registries"], after["registries"]
    if edit.owner not in old or old.keys() != new.keys():
        return None
    changes = {}
    for owner, old_state in old.items():
        new_state = new[owner]
        if any(not isinstance(state, dict) or not isinstance(state.get("entries"), dict)
               or not isinstance(state.get("keys"), list)
               or not all(isinstance(key, str) for key in state["keys"])
               or len(state["keys"]) != len(state["entries"]) or set(state["keys"]) != state["entries"].keys()
               for state in (old_state, new_state)):
            return None
        left, right = old_state["entries"], new_state["entries"]
        common = left.keys() & right.keys()
        if [k for k in old_state["keys"] if k in common] != [k for k in new_state["keys"] if k in common]:
            return None
        changed = (left.keys() ^ right.keys()) | {key for key in common if left[key] != right[key]}
        if not changed <= edit.keys:
            return None
        if any(not isinstance(table[key], str) for key in edit.keys for table in (left, right) if key in table):
            return None
        if changed:
            changes[owner] = changed
    return changes


def import_candidates(index, applied, inspect_tree):
    data = index.index.get("round3_imports")
    if not data or data.get("revision") != PINNED or data.get("tests") != sorted(index.index["tests"]):
        return None
    edit = registry_edit(applied)
    if edit is None:
        return None
    result = inspect_tree((edit.module,))
    if not result:
        return None
    changes = changed_registry_keys(*result, edit)
    if changes is None:
        return None
    consumers = set(data["observers"])
    for owner, keys in changes.items():
        blocked = data["blocked"].get(owner, ())
        if keys.intersection(blocked):
            return None
        for key in keys:
            consumers.update(data["readers"].get(owner, {}).get(key, ()))
    readers = int(index.index["files"][edit.path]["readers"], 16)
    observers = int(index.position_observers or "0", 16)
    selected = readers | observers | index.uncovered
    for bit, test, _ in index.known:
        if test["id"] in consumers:
            selected |= bit
    detail = ",".join(sorted({key for keys in changes.values() for key in keys}))
    return selected, f"cold-registry-readers:{detail or 'same-state'}"


def collect(root):
    import pytest

    root = Path(root).resolve()
    sys.path.insert(0, str(root))
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    failed_files = set()
    collected = set()

    class Recorder:
        def pytest_collectreport(self, report):
            if report.failed:
                failed_files.add(report.nodeid)

        def pytest_collection_modifyitems(self, items):
            collected.update(item.nodeid for item in items)

    status = pytest.main(["tests", "--collect-only", "-q", "-p", "no:cacheprovider",
                          "--continue-on-collection-errors"], plugins=[Recorder()])
    return {"version": SCHEMA, "complete": status in (0, 1, 2) and bool(collected),
            "failed_files": sorted(failed_files), "collected": sorted(collected)}


def collection_partition(report, tests):
    """Separate confirmed failed-file nodes and still-unclassified runtime nodes."""
    if (not isinstance(report, dict) or report.get("version") != SCHEMA or report.get("complete") is not True
            or not all(isinstance(report.get(key), list) and all(isinstance(item, str) for item in report[key])
                       for key in ("failed_files", "collected"))):
        return None
    files = set(report["failed_files"])
    nodes = set(report["collected"])
    collected = set()
    errored = set()
    for test in tests:
        from_error = test["file"] in files
        is_collected = test["id"] in nodes
        if from_error == is_collected:
            return None
        group = errored if from_error else collected
        group.add(test["id"])
    return errored, collected


def main():
    operation, root, output, *modules = sys.argv[1:]
    output = Path(output).resolve()
    if operation == "snapshot" and modules:
        source = Path(root).resolve()
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always", SyntaxWarning)
            result = snapshot(source, modules)
        result["warnings"] = sorted([
            (os.path.relpath(warning.filename, source), warning.lineno, warning.category.__name__, str(warning.message))
            for warning in recorded
        ])
    elif operation == "collect" and not modules:
        result = collect(root)
    else:
        raise SystemExit("expected snapshot <root> <output> <module>... or collect <root> <output>")
    output.write_text(json.dumps(result, separators=(",", ":"), sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
