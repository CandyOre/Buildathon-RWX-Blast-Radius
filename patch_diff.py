"""Read and verify independent unified diffs without changing the checkout."""

import re
from dataclasses import dataclass


HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?")


class UnsupportedPatch(ValueError):
    """The selector cannot establish which baseline code this patch changes."""


@dataclass(frozen=True)
class Hunk:
    old_offset: int
    changes: tuple[str, ...]


@dataclass(frozen=True)
class FilePatch:
    path: str
    hunks: tuple[Hunk, ...]

    def apply(self, source: str) -> str:
        old_lines = source.split("\n")
        new_lines = []
        cursor = 0
        for hunk in self.hunks:
            if not cursor <= hunk.old_offset <= len(old_lines):
                raise UnsupportedPatch("hunk offset outside baseline file")
            new_lines.extend(old_lines[cursor:hunk.old_offset])
            cursor = hunk.old_offset
            for line in hunk.changes:
                prefix, payload = line[0], line[1:]
                if prefix != "+":
                    if cursor >= len(old_lines) or old_lines[cursor] != payload:
                        raise UnsupportedPatch("diff context does not match the baseline")
                    cursor += 1
                if prefix != "-":
                    new_lines.append(payload)
        new_lines.extend(old_lines[cursor:])
        return "\n".join(new_lines)


def parse_patch(text: str) -> list[FilePatch]:
    files = []
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        if not lines[index] and index == len(lines) - 1:
            break
        header = None
        if lines[index].startswith("diff --git "):
            header = lines[index]
            index += 1
        if index < len(lines) and lines[index].startswith("index "):
            index += 1
        if index + 1 >= len(lines) or not lines[index].startswith("--- a/"):
            raise UnsupportedPatch("expected a modification of an existing file")
        old_path = lines[index][6:]
        if not old_path or lines[index + 1] != f"+++ b/{old_path}":
            raise UnsupportedPatch("path changed or is quoted")
        if header and header != f"diff --git a/{old_path} b/{old_path}":
            raise UnsupportedPatch("inconsistent diff file headers")
        index += 2
        hunks = []
        delta = 0
        while index < len(lines) and (match := HUNK.fullmatch(lines[index])):
            old_start, old_size, new_start, new_size = (int(part) if part is not None else 1
                                                       for part in match.groups())
            old_offset = old_start if not old_size else old_start - 1
            new_offset = new_start if not new_size else new_start - 1
            if old_offset < 0 or new_offset != old_offset + delta:
                raise UnsupportedPatch("inconsistent hunk offsets")
            delta += new_size - old_size
            index += 1
            old_remaining, new_remaining = old_size, new_size
            changes = []
            while old_remaining or new_remaining:
                if index >= len(lines) or lines[index][:1] not in (" ", "+", "-"):
                    raise UnsupportedPatch("truncated diff hunk")
                line = lines[index]
                changes.append(line)
                old_remaining -= not line.startswith("+")
                new_remaining -= not line.startswith("-")
                index += 1
                if min(old_remaining, new_remaining) < 0:
                    raise UnsupportedPatch("invalid diff hunk length")
            if not changes:
                raise UnsupportedPatch("empty diff hunk")
            hunks.append(Hunk(old_offset, tuple(changes)))
        if not hunks or any(file.path == old_path for file in files):
            raise UnsupportedPatch("missing or duplicate file change")
        files.append(FilePatch(old_path, tuple(hunks)))
    if not files:
        raise UnsupportedPatch("empty patch")
    return files
