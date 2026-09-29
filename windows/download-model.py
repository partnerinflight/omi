"""Setup-time downloads only; runtime is offline. Never downloads user audio."""
import argparse
import json
import re
from pathlib import Path
from huggingface_hub import snapshot_download

p = argparse.ArgumentParser()
p.add_argument('--lock', required=True, type=Path)
p.add_argument('--model', required=True, choices=['moss_model', 'vibe_model', 'speaker_model'])
p.add_argument('--destination', required=True)
a = p.parse_args()
model = json.loads(a.lock.read_text(encoding='utf-8-sig'))[a.model]
if not re.fullmatch(r'[0-9a-f]{40}', model['revision']):
    raise ValueError('Model revision must be a full commit SHA')
snapshot_download(model['repo'], revision=model['revision'], local_dir=a.destination,
                  allow_patterns=model['patterns'])
print(f"Downloaded {model['repo']} at {model['revision']}")
