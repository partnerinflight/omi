#!/usr/bin/env python3
"""Verify wheel dependency resolution and run packaged processing outside the repo.

Uses synthetic audio and a deterministic ASR fixture, never real models/audio.
Requires ffmpeg/ffprobe on PATH; pip may download declared dependencies.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv

root = Path(__file__).resolve().parents[1]
wheels = Path(sys.argv[1]).resolve()
package, = wheels.glob('second_brain_local-*.whl')
with tempfile.TemporaryDirectory(prefix='second-brain-installed-') as tmp:
    work = Path(tmp)
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    venv.create(work / 'python', with_pip=True)
    python = work / 'python' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    def run(args):
        subprocess.run([str(python), *map(str, args)], check=True, cwd=work, env=env)
    # Only name the main wheel: the receiver must resolve via package metadata.
    run(['-m', 'pip', 'install', '--find-links', wheels, package])
    run(['-m', 'pip', 'check'])
    run(['-I', '-c', 'import second_brain.runtime, second_brain.adaptive.pipeline, omi_local.server; print("Installed imports OK")'])
    run(['-I', '-m', 'second_brain.cli', '--help'])
    run(['-I', '-m', 'omi_local.cli', '--help'])
    shutil.copyfile(root / 'tests/fixtures/fake_moss.py', work / 'fake_moss.py')
    audio = work / 'synthetic.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=8', '-ar', '16000', '-ac', '1', str(audio)], check=True)
    cfg = json.loads((root / 'config/pipeline.example.json').read_text())
    cfg.update(moss_command=[str(python), str(work / 'fake_moss.py')],
               moss_cpp_engine_dir=str(work), moss_model=str(work / 'fixture.gguf'),
               scan_vault_for_novelty=False, vault_path=str(work / 'vault'),
               speaker_python=None, speaker_model=None, hermes_scoring_enabled=False)
    (work / 'vault').mkdir()
    (work / 'pipeline.json').write_text(json.dumps(cfg), encoding='utf-8')
    run(['-I', '-m', 'second_brain.adaptive.pipeline', '--audio', audio,
         '--config', work / 'pipeline.json', '--output-dir', work / 'results',
         '--skip-vibe7', '--no-hermes'])
    manifest = json.loads((work / 'results/manifest.json').read_text())
    assert manifest['windows'], 'Installed pipeline produced no windows'
    assert any(w['memory_keep'] for w in manifest['windows']), 'Fixture durable content was lost'
    assert manifest['speaker_observations'], 'Installed speaker clip preparation did not run'
    assert list((work / 'results/speaker-clips').glob('*.wav')), 'No playable clips'
    print('PASS: clean installed packages, receiver dependency, pipeline, gate, and review clips')
