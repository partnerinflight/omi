"""Obsidian-native comments, with compatibility for older HTML event markers."""
import re

MARKER = re.compile(r'(?:<!-- router:|%% router:)([a-f0-9]+)(?: -->| %%)')


def marker(eid: str) -> str:
    return f'%% router:{eid} %%'


def normalize(text: str) -> str:
    return MARKER.sub(lambda m: marker(m[1]), text)
