"""Discard only trailing module pass statements that the compiler corroborates."""

import ast
import sys
import warnings
from types import CodeType


CODE_FIELDS = (
    "co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_nlocals", "co_stacksize",
    "co_flags", "co_code", "co_names", "co_varnames", "co_freevars", "co_cellvars", "co_name",
    "co_qualname", "co_exceptiontable", "co_filename",
)


def same_compiled_code(left, right, *, module=False):
    """Recursively compare executable metadata and preserve nested positions."""
    if any(getattr(left, name) != getattr(right, name) for name in CODE_FIELDS):
        return False
    if not module and (left.co_firstlineno, left.co_linetable) != (right.co_firstlineno, right.co_linetable):
        return False
    if len(left.co_consts) != len(right.co_consts):
        return False
    for old, new in zip(left.co_consts, right.co_consts):
        if type(old) is not type(new):
            return False
        if isinstance(old, CodeType):
            if not same_compiled_code(old, new):
                return False
        elif old != new:
            return False
    return True


def compile_source(source, path):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        code = compile(source.encode("utf-8"), path, "exec", dont_inherit=True, optimize=0)
    return code, [(warning.category, str(warning.message), warning.lineno) for warning in caught]


def trailing_passes(module):
    tail = []
    for node in reversed(module.body):
        if not isinstance(node, ast.Pass):
            break
        tail.append(node)
    return tail


def normalize_module_tail(old, new, source, path):
    """Return a comparison AST and whether tests can see different module positions."""
    tail = trailing_passes(new)
    extra = len(tail) - len(trailing_passes(old))
    if extra <= 0:
        return None, ""
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 13):
        return None, "trailing module pass requires CPython 3.13"
    lines = source.split("\n")
    for stmt in tail[:extra]:
        if stmt.lineno != stmt.end_lineno or lines[stmt.lineno - 1].split("#", 1)[0].strip() != "pass":
            return None, "trailing pass is not a standalone statement"
        lines[stmt.lineno - 1] = ""
    amended = "\n".join(lines)
    before, before_warnings = compile_source(source, path)
    after, after_warnings = compile_source(amended, path)
    if before_warnings != after_warnings or not same_compiled_code(before, after, module=True):
        return None, "trailing pass changes bytecode, nested positions or warnings"
    positions_changed = (before.co_firstlineno, before.co_linetable) != (after.co_firstlineno, after.co_linetable)
    return (ast.parse(amended.encode("utf-8"), filename=path, type_comments=True), positions_changed), ""
