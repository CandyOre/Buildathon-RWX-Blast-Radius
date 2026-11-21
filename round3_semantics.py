"""Conservative whole-file compiler proofs and specific observed control edits."""

import ast
import copy
import dis
import operator
import sys
from collections import Counter
from types import CodeType

from equivalent_edits import CODE_FIELDS, compile_source


OPERATOR_NAMES = {ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}
OPERATIONS = {name: getattr(operator, name) for name in OPERATOR_NAMES.values()}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
REFLECTIVE = {
    "locals", "vars", "dir", "eval", "exec", "compile", "f_locals", "_getframe", "currentframe",
    "settrace", "setprofile", "stack", "getclosurevars",
}
PURE_LITERALS = (ast.Constant, ast.Tuple, ast.Load, ast.BinOp, ast.UnaryOp,
                 ast.Add, ast.Sub, ast.Mult, ast.LShift, ast.RShift, ast.BitOr, ast.BitAnd, ast.BitXor,
                 ast.UAdd, ast.USub, ast.Invert)


def syntax(node):
    if isinstance(node, ast.AST):
        return ast.dump(node)
    if isinstance(node, list):
        return tuple(syntax(item) for item in node)
    return node


def positions(code):
    return (code.co_firstlineno, code.co_linetable,
            tuple(positions(const) for const in code.co_consts if isinstance(const, CodeType)))


def generated_class_location(code, index, value):
    """Recognize a position constant used only by CPython's class prologue."""
    if (type(value) is not int or value != code.co_firstlineno or code.co_flags & 0x1
            or "__firstlineno__" not in code.co_names):
        return False
    instructions = list(dis.get_instructions(code))
    prologue = [
        ("RESUME", 0), ("LOAD_NAME", "__name__"), ("STORE_NAME", "__module__"),
        ("LOAD_CONST", code.co_qualname), ("STORE_NAME", "__qualname__"),
        ("LOAD_CONST", value), ("STORE_NAME", "__firstlineno__"),
    ]
    if [(instruction.opname, instruction.argval) for instruction in instructions[:7]] != prologue:
        return False
    uses = [offset for offset, instruction in enumerate(instructions)
            if instruction.opcode in dis.hasconst and instruction.arg == index]
    return uses == [5]


def code_equal(before, after, *, allow_locals=False):
    fields = (field for field in CODE_FIELDS if not allow_locals or field != "co_varnames")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        return False
    if len(before.co_consts) != len(after.co_consts):
        return False
    for index, (left, right) in enumerate(zip(before.co_consts, after.co_consts)):
        if type(left) is not type(right):
            return False
        if isinstance(left, CodeType):
            if not code_equal(left, right, allow_locals=allow_locals):
                return False
        elif isinstance(left, tuple):
            if not literal_equal(left, right):
                return False
        elif left != right or repr(left) != repr(right):
            if not (generated_class_location(before, index, left) and generated_class_location(after, index, right)):
                return False
    return True


def literal_equal(before, after):
    if type(before) is not type(after):
        return False
    if isinstance(before, tuple):
        return len(before) == len(after) and all(literal_equal(a, b) for a, b in zip(before, after))
    return before == after and repr(before) == repr(after)


def compiler(source, tree, path):
    code, warnings = compile_source(source, path)
    if tree is not None:
        represented = compile(tree, path, "exec", dont_inherit=True, optimize=0)
        if not code_equal(code, represented) or positions(code) != positions(represented):
            return None, warnings
    return code, warnings


class LocalNames(ast.NodeTransformer):
    def __init__(self, translations):
        self.translations = translations

    def visit_Name(self, node):
        return ast.copy_location(ast.Name(self.translations.get(node.id, node.id), node.ctx), node)


def alpha_function(before, after, previous_code, new_code):
    if type(before) is not type(after) or not isinstance(before, FUNCTIONS):
        return False
    if before.decorator_list or after.decorator_list or previous_code.co_cellvars or previous_code.co_freevars:
        return False
    if any(syntax(value) != syntax(getattr(after, field)) for field, value in ast.iter_fields(before)
           if field != "body"):
        return False
    arguments = (previous_code.co_argcount + previous_code.co_kwonlyargcount
                 + bool(previous_code.co_flags & 0x4) + bool(previous_code.co_flags & 0x8))
    old_names, new_names = previous_code.co_varnames, new_code.co_varnames
    if old_names[:arguments] != new_names[:arguments] or len(old_names) != len(new_names):
        return False
    translations = {right: left for left, right in zip(old_names[arguments:], new_names[arguments:]) if left != right}
    if not translations:
        return False
    for scope in (before, after):
        for part in scope.body:
            for node in ast.walk(part):
                if isinstance(node, (*FUNCTIONS, ast.Lambda, ast.ClassDef, ast.Global, ast.Nonlocal,
                                     ast.Import, ast.ImportFrom, ast.ExceptHandler, ast.Match)):
                    return False
                name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", None)
                if name in REFLECTIVE:
                    return False
    translated = LocalNames(translations).visit(copy.deepcopy(after))
    return syntax(before) == syntax(translated) and code_equal(previous_code, new_code, allow_locals=True)


def module_function_codes(code):
    return {c.co_name: c for c in code.co_consts if isinstance(c, CodeType) and c.co_name == c.co_qualname}


def equivalent_literal(before, after, path):
    if not all(isinstance(node, PURE_LITERALS) for tree in (before, after) for node in ast.walk(tree)):
        return False
    old = compile(ast.Expression(before), path, "eval", dont_inherit=True, optimize=0)
    new = compile(ast.Expression(after), path, "eval", dont_inherit=True, optimize=0)
    return code_equal(old, new)


def align_fragments(before, after, path):
    if comparable_literal(before, after) and equivalent_literal(before, after, path):
        return before, True
    if type(before) is not type(after):
        return after, False
    if isinstance(before, ast.AST):
        replacement = copy.copy(after)
        changed = False
        for name, previous in ast.iter_fields(before):
            value, edit = align_fragments(previous, getattr(after, name), path)
            if edit:
                setattr(replacement, name, value)
                changed = True
        return (replacement if changed else after), changed
    if isinstance(before, list) and len(before) == len(after):
        pairs = [align_fragments(a, b, path) for a, b in zip(before, after)]
        if any(changed for _, changed in pairs):
            return [value for value, _ in pairs], True
    return after, False


def comparable_literal(before, after):
    foldable = (ast.BinOp, ast.UnaryOp)
    return ((isinstance(before, foldable) or isinstance(after, foldable))
            and isinstance(before, ast.expr) and isinstance(after, ast.expr)
            and syntax(before) != syntax(after))


def function_pairs(old, new):
    old_definitions = [stmt for stmt in old.body if isinstance(stmt, FUNCTIONS)]
    new_definitions = [stmt for stmt in new.body if isinstance(stmt, FUNCTIONS)]
    old_counts = Counter(stmt.name for stmt in old_definitions)
    new_counts = Counter(stmt.name for stmt in new_definitions)
    previous = {stmt.name: stmt for stmt in old_definitions if old_counts[stmt.name] == 1}
    for index, after in enumerate(new.body):
        if not isinstance(after, FUNCTIONS) or new_counts[after.name] != 1:
            continue
        before = previous.get(after.name)
        if (type(before) is type(after) and syntax(before) != syntax(after)
                and all(syntax(value) == syntax(getattr(after, field)) for field, value in ast.iter_fields(before)
                        if field != "body")):
            yield index, before, after


def mix_candidates(old, new, pairs, old_code, new_code, path):
    """Replace aligned function bodies only after independently proving equivalence."""
    old_functions, new_functions = module_function_codes(old_code), module_function_codes(new_code)
    body = new.body.copy()
    changed = False
    alpha = False
    for position, before, after in pairs:
        previous_code = old_functions.get(before.name)
        current_code = new_functions.get(after.name)
        if previous_code is not None and current_code is not None:
            renamed = alpha_function(before, after, previous_code, current_code)
            if renamed or code_equal(previous_code, current_code):
                # Keep the patched declaration and defaults at their actual
                # location. Only the independently equivalent body changes.
                replacement = copy.copy(after)
                replacement.body = before.body
                body[position] = replacement
                changed = True
                alpha |= renamed
            else:
                body[position], fragments = align_fragments(before, after, path)
                changed |= fragments
    if changed:
        amended = ast.Module(body=body, type_ignores=new.type_ignores)
        if alpha and syntax(old) != syntax(amended):
            return None
        compiled = compile(amended, path, "exec", dont_inherit=True, optimize=0)
        if code_equal(new_code, compiled, allow_locals=alpha):
            return amended, "compiled-equivalent-local-names" if alpha else "compiled-equivalent-function", True
    return None


def normalize_python(before_source, after_source, path, old, new):
    """Return an equivalent analysis view and the reader/observer requirement."""
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 13):
        return None
    tags = [item.tag for item in old.type_ignores]
    moved_tags = tags == [item.tag for item in new.type_ignores] and syntax(old.type_ignores) != syntax(new.type_ignores)
    if moved_tags:
        aligned = ast.Module(body=new.body, type_ignores=old.type_ignores)
    else:
        aligned = new
    old_syntax = syntax(old)
    if old_syntax == syntax(aligned):
        old_code, old_warnings = compiler(before_source, old, path)
        new_code, new_warnings = compiler(after_source, new, path)
        if (old_code is None or new_code is None or old_warnings != new_warnings
                or not code_equal(old_code, new_code)):
            raise ValueError(f"{path}: source changes compile behavior or warnings")
        return old, "compiled-layout", moved_tags or positions(old_code) != positions(new_code)

    # Compile substantive changes only when a same-shaped body or declaration
    # could carry a separately removable fragment.
    pairs = list(function_pairs(old, aligned))
    moved_functions = any(
        isinstance(a, FUNCTIONS) and type(a) is type(b) and a.name == b.name and a.lineno != b.lineno
        for a, b in zip(old.body, aligned.body)
    )
    if not moved_tags and not moved_functions and not pairs:
        return None
    old_code, old_warnings = compiler(before_source, old, path)
    new_code, new_warnings = compiler(after_source, new, path)
    if old_code is None or new_code is None or old_warnings != new_warnings:
        return None
    if code_equal(old_code, new_code):
        return old, "compiled-equivalent-python", positions(old_code) != positions(new_code)
    corrected = mix_candidates(old, aligned, pairs, old_code, new_code, path)
    if corrected:
        return corrected
    if moved_tags:
        compared = compile(aligned, path, "exec", dont_inherit=True, optimize=0)
        if code_equal(new_code, compared) and positions(new_code) == positions(compared):
            return aligned, "compiled-type-ignore-locations", True
    if moved_functions:
        old_functions, new_functions = module_function_codes(old_code), module_function_codes(new_code)
        if any(code_equal(old_functions[name], new_functions[name])
               and positions(old_functions[name]) != positions(new_functions[name])
               for name in old_functions.keys() & new_functions.keys()):
            return new, "compiled-mixed-positions", True
    return None


def tuple_literal(node):
    if not isinstance(node, ast.Tuple) or len(node.elts) > 8:
        return None
    if all(isinstance(value, ast.Constant) and type(value.value) is int for value in node.elts):
        return tuple(value.value for value in node.elts)
    return None


def version_spec(node):
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        operation = OPERATOR_NAMES.get(type(node.ops[0]))
        version = tuple_literal(node.comparators[0])
        if operation and version is not None:
            return operation, version
    return None


def token_spec(node):
    if not isinstance(node, ast.Call) or len(node.args) != 1:
        return None
    if not (isinstance(node.func, ast.Attribute) and node.func.attr == "_match"
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"):
        return None
    token = node.args[0]
    if not isinstance(token, ast.Attribute) or not isinstance(token.value, ast.Name) or token.value.id != "TokenType":
        return None
    if not node.keywords:
        return token.attr, True
    if len(node.keywords) == 1 and node.keywords[0].arg == "advance":
        value = node.keywords[0].value
        if isinstance(value, ast.Constant) and type(value.value) is bool:
            return token.attr, value.value
    return None


def single_control_change(before, after):
    """Align one changed comparison or token target across the complete scope."""
    change = None
    stack = [(before, after, False)]
    while stack:
        old, new, short_circuit = stack.pop()
        if type(old) is not type(new):
            return None
        if isinstance(old, (ast.Compare, ast.Call)) and syntax(old) != syntax(new):
            left, right = version_spec(old), version_spec(new)
            if left and right and syntax(old.left) == syntax(new.left):
                candidate = "version", left, right
            else:
                left, right = token_spec(old), token_spec(new)
                candidate = ("match", left, right) if left and right and left[1] == right[1] else None
            if candidate:
                if change is not None:
                    return None
                change = f"{old.lineno}:{old.col_offset}", *candidate, short_circuit
                continue
        if isinstance(old, ast.AST):
            enclosing = short_circuit or isinstance(old, ast.BoolOp)
            stack.extend((value, getattr(new, field), enclosing) for field, value in ast.iter_fields(old))
        elif isinstance(old, list) and len(old) == len(new):
            stack.extend((a, b, short_circuit) for a, b in zip(old, new))
        elif not literal_equal(old, new):
            return None
    return change


def control_masks(path, before, after, impact_index):
    """Return witnesses, unaffected contexts, and the rule used."""
    data = impact_index.get("round3_controls", {})
    sites = data.get("files", {}).get(path)
    change = single_control_change(before, after) if sites else None
    if not change:
        return None
    location, kind, old, new, short_circuit = change
    site = sites.get(location)
    if not site or site["kind"] != kind or site.get("blocked"):
        return None
    if kind == "match":
        tokens = data["tokens"]
        if old[0] not in tokens or new[0] not in tokens:
            return None
        prior, proposed = tokens[old[0]], tokens[new[0]]

        def diverges(value):
            return (value == prior) != (value == proposed)
    else:
        prior, proposed = OPERATIONS[old[0]], OPERATIONS[new[0]]

        def diverges(value):
            observed = tuple(value)
            return prior(observed, old[1]) != proposed(observed, new[1])

    witnesses, unaffected = int(site["incomplete"], 16), 0
    for value, mask, shared in site["samples"]:
        bits = int(mask, 16)
        if diverges(value):
            if shared:
                return None
            witnesses |= bits
        else:
            unaffected |= bits
    reason = f"observed-{kind}-inputs"
    if short_circuit:
        reason += "-in-short-circuit"
    return witnesses, unaffected & ~witnesses, reason
