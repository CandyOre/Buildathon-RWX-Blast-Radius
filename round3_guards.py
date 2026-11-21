"""Prove an inserted literal early return inactive on fully observed inputs."""

import ast
import copy


def integer_literal(node):
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant) and type(node.operand.value) is int):
        return -node.operand.value
    return None


def literal_return(statement):
    if not isinstance(statement, ast.Return) or statement.value is None:
        return False
    return all(isinstance(node, (ast.Constant, ast.Tuple, ast.List, ast.Load))
               for node in ast.walk(statement.value))


def guard_proof(path, old, after, impact_index):
    sites = impact_index.get("round3_entry_guards", {}).get(path, {})
    if not sites or len(old.body) != len(after.body):
        return False
    if [node.tag for node in old.type_ignores] != [node.tag for node in after.type_ignores]:
        return False
    pairs = [(left, right) for left, right in zip(old.body, after.body) if ast.dump(left) != ast.dump(right)]
    if len(pairs) != 1:
        return False
    before, rewritten = pairs[0]
    if not isinstance(before, ast.FunctionDef) or not isinstance(rewritten, ast.FunctionDef):
        return False
    observed = sites.get(str(before.lineno), {})
    start = 1 if ast.get_docstring(before, clean=False) is not None else 0
    if not observed or len(rewritten.body) != len(before.body) + 1:
        return False
    inserted = rewritten.body[start]
    if (not isinstance(inserted, ast.If) or inserted.orelse or len(inserted.body) != 1
            or not literal_return(inserted.body[0])):
        return False
    test = inserted.test
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.left, ast.Name) and test.left.id in observed):
        return False
    value = integer_literal(test.comparators[0])
    low, high = observed[test.left.id]
    if value is None or low <= value <= high:
        return False
    restored = copy.copy(rewritten)
    restored.body = rewritten.body[:start] + rewritten.body[start + 1:]
    return ast.dump(before) == ast.dump(restored)
