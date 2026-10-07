from __future__ import annotations
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from .config import Config


def meeting_decoder_problem(ffmpeg="ffmpeg"):
    """None if ffmpeg lists an Opus decoder (the Mac app's CAF codec), else a short problem."""
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-decoders"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "ffmpeg is not runnable; meeting captures cannot be decoded"
    names = {line.split()[1] for line in proc.stdout.splitlines() if len(line.split()) > 1}
    if proc.returncode or not names & {"opus", "libopus"}:
        return "ffmpeg has no Opus decoder; meeting captures cannot be decoded"
    return None


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
    if cfg.meetings_enabled:
        search = cfg.ffmpeg_dir + os.pathsep + os.environ.get("PATH", "") if cfg.ffmpeg_dir else None
        ffmpeg = shutil.which("ffmpeg", path=search)
        if ffmpeg and (problem := meeting_decoder_problem(ffmpeg)):
            errors.append(problem)
    for key in ["moss_model", "moss_cpp_engine_dir"]:
        if not Path(pipeline.get(key, "")).is_absolute() or not Path(pipeline.get(key, "")).exists():
            errors.append(f"{key} does not exist")
    exe = Path(pipeline.get("moss_cpp_engine_dir", "")) / (
        "moss-transcribe.exe" if os.name == "nt" else "moss-transcribe"
    )
    if not exe.is_file():
        errors.append("MOSS executable is missing")
    if not cfg.skip_vibe7:
        repo = Path(pipeline.get("vibe_repo") or "")
        python = Path(pipeline.get("vibe_python") or repo / ".venv" / "Scripts" / "python.exe")
        if not repo.is_absolute() or not repo.is_dir():
            errors.append("VibeVoice repository must be an existing absolute directory")
        if not python.is_absolute() or not python.is_file():
            errors.append("VibeVoice Python environment is missing")
        model = Path(pipeline.get("vibe_7b_model") or "")
        if not model.is_absolute() or not model.is_dir():
            errors.append("Download VibeVoice to a local model path before unattended service use")
    if pipeline.get("speaker_model") or pipeline.get("speaker_python"):
        python = Path(pipeline.get("speaker_python") or "")
        model = Path(pipeline.get("speaker_model") or "")
        if not python.is_absolute() or not python.is_file():
            errors.append("Speaker encoder Python environment is missing")
        for name in (
            "hyperparams.yaml",
            "embedding_model.ckpt",
            "mean_var_norm_emb.ckpt",
            "classifier.ckpt",
            "label_encoder.txt",
        ):
            if not model.is_absolute() or not (model / name).is_file():
                errors.append("Speaker model is incomplete; run windows/setup-speakers.ps1")
                break
    for name, path in [
        ("vault", cfg.vault_path / cfg.vault_folder),
        ("data", cfg.data_dir),
        ("incoming", cfg.incoming_dir),
    ]:
        try:
            if name == "vault" and not cfg.vault_path.is_dir():
                raise FileNotFoundError()
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=path):
                pass
        except OSError:
            errors.append(f"{name} is unavailable or not writable by this account")
    return {"ok": not errors, "errors": errors}
