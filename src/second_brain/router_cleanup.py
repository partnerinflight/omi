"""Backed-up cleanup of router-owned bullets; curated prose is left untouched."""
from __future__ import annotations

import re
from pathlib import Path

from .router import MANAGED_HEADER

MARKER = re.compile(r"<!-- router:([a-f0-9]+) -->")


def clean_note(text: str, remove: set[str]) -> tuple[str, list[str]]:
    lines = text.splitlines(keepends=True)
    output, removed = [], []
    managed = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == MANAGED_HEADER:
            managed = True
        elif line.startswith("## "):
            managed = False
        if managed and line.startswith("- "):
            end = i + 1
            while end < len(lines) and lines[end].startswith(("  ", "&#x20;")):
                end += 1
            block = "".join(lines[i:end])
            markers = MARKER.findall(block)
            if len(markers) == 1:
                eid = markers[0]
                if eid in remove:
                    removed.append(eid)
                else:
                    # Only strip the router's provenance continuation, never arbitrary prose.
                    body = MARKER.sub("", line).rstrip("\r\n")
                    source_lines = lines[i + 1:end]
                    if all(re.match(r"^(?:  |&#x20;)*Source:", s) for s in source_lines):
                        body = body.replace("&#x20;", " ").rstrip().removesuffix("\\").rstrip()
                        output.append(f"{body} <!-- router:{eid} -->\n")
                    else:
                        output.append(block)
                i = end
                continue
        output.append(line)
        i += 1
    return "".join(output), removed
