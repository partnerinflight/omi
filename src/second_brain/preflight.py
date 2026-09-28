from __future__ import annotations
import os
import shutil
import tempfile
from pathlib import Path
from .config import Config


def check(cfg: Config):
    errors = []
    pipeline = cfg.pipeline()
    from omi_local.server import load_or_create_secret

    try:
        load_or_create_secret(cfg.secret_file, create=False)
    except (OSError, ValueError):
        errors.append("Pairing key is missing or invalid; import the existing receiver key")
    for name in ["ffmpeg", "ffprobe"]:
        search = cfg.ffmpeg_dir + os.pathsep + os.environ.get("PATH", "") if cfg.ffmpeg_dir else None
        if not shutil.which(name, path=search):
            errors.append(f"{name} is unavailable to this account")
    for key in ["moss_model", "moss_cpp_engine_dir"]:
        if not Path(pipeline.get(key, "")).exists():
            errors.append(f"{key} does not exist")
    exe = Path(pipeline.get("moss_cpp_engine_dir", "")) / (
        "moss-transcribe.exe" if os.name == "nt" else "moss-transcribe"
    )
    if not exe.is_file():
        errors.append("MOSS executable is missing")
    if not cfg.skip_vibe7:
        python = pipeline.get("vibe_python") or str(Path(pipeline["vibe_repo"]) / ".venv" / "Scripts" / "python.exe")
        if not Path(python).is_file():
            errors.append("VibeVoice Python environment is missing")
        if not Path(pipeline["vibe_7b_model"]).exists():
            errors.append("Download VibeVoice to a local model path before unattended service use")
    for name, path in [("vault", cfg.vault_path), ("data", cfg.data_dir), ("incoming", cfg.incoming_dir)]:
        try:
            if name == "vault" and not path.is_dir():
                raise FileNotFoundError()
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=path):
                pass
        except OSError:
            errors.append(f"{name} is unavailable or not writable by this account")
    return {"ok": not errors, "errors": errors}
