"""Deterministic daily checklist housekeeping; never infer completion."""
from __future__ import annotations

import re
import copy

TASK = re.compile(r"^[-*+][ \t]+\[([ xX-])\][ \t]+")
HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*\r?\n?$")


def organize(text: str) -> tuple[str, dict[str, int]]:
    """Move checked task blocks down and reopened blocks up, keeping their bytes.

    Indented notes/subtasks travel with their parent. Free prose and other sections
    stay intact. Only the explicitly managed Tasks/Completed sections are sorted.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    sections = [[None, [], ""]]
    fence = None
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not stripped[len(token):].strip():
                fence = None
            sections[-1][1].append(line)
            continue
        heading = HEADING.match(line) if fence is None else None
        if heading:
            sections.append([heading[1], [], line])
        else:
            sections[-1][1].append(line)
    if fence:
        raise ValueError("Unclosed code fence in ToDos; refusing to reorganize")
    for name in ("Tasks", "Completed"):
        if sum(section[0] == name for section in sections) > 1:
            raise ValueError(f"Duplicate {name} sections; refusing to reorganize")
    active = next((section for section in sections if section[0] == "Tasks"), None)
    if active is None:
        raise ValueError("Missing ## Tasks heading; refusing to guess the task section")
    completed = next((section for section in sections if section[0] == "Completed"), None)
    if completed is None:
        completed = ["Completed", [], f"## Completed{newline}"]
        sections.append(completed)

    def extract(lines, move_done):
        kept, moved = [], []
        i, fenced = 0, None
        while i < len(lines):
            line = lines[i]
            marker = re.match(r"(`{3,}|~{3,})", line.lstrip())
            if marker:
                token = marker[1]
                if fenced is None:
                    fenced = token
                elif token[0] == fenced[0] and len(token) >= len(fenced) and not line.lstrip()[len(token):].strip():
                    fenced = None
            match = TASK.match(line) if fenced is None and not marker else None
            end = i + 1
            if match:
                # Keep nested checkboxes and indented notes with their parent.
                while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t"))):
                    end += 1
            block = lines[i:end]
            if match and (match[1].lower() == "x") == move_done:
                moved.append(block)
            else:
                kept.extend(block)
            i = end
        return kept, moved

    active[1], done = extract(active[1], True)
    completed[1], reopened = extract(completed[1], False)
    for destination, blocks in ((active, reopened), (completed, done)):
        for block in blocks:
            if destination[1] and not destination[1][-1].endswith(("\n", "\r")):
                destination[1].append(newline)
            destination[1].extend(block)
    sections.remove(completed)
    sections.append(completed)
    output = ""
    for name, lines, heading in sections:
        if heading and output and not output.endswith(newline * 2):
            output += newline if output.endswith(newline) else newline * 2
        output += heading + "".join(lines)
    return output, {"moved_to_completed": len(done), "moved_to_tasks": len(reopened)}


def scrub(vault):
    from pathlib import Path
    from .checklist import StateStore
    from .todos import TodoFile

    root = Path(vault).expanduser().resolve(strict=True)
    store, todos = StateStore(root), TodoFile(root)
    with store.locked():
        state = store.read()
        prior_state = copy.deepcopy(state)
        before = todos.read()
        if before is None:
            raise ValueError("ToDos/Tasks.md is missing")
        # Reject malformed/duplicate identifiers before touching the document.
        todos.rows(before)
        after, counts = organize(before)
        todos.rows(after)
        if after != before:
            todos.write(before, after)
        todos.reconcile(state)
        if state != prior_state:
            store.write(state)
    return {"success": True, "changed": before != after, **counts}
