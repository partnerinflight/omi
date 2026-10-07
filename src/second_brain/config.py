from __future__ import annotations
from dataclasses import dataclass
import json
import os
from pathlib import Path


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def check_folder(key: str, value: str) -> None:
    folder = value.replace("\\", "/")
    if (
        not folder
        or Path(folder).is_absolute()
        or any(x in ("..", ".", "") for x in folder.split("/"))
        or ":" in folder
    ):
        raise ValueError(f"{key} must be a safe relative folder")


@dataclass(frozen=True)
class Config:
    data_dir: Path
    incoming_dir: Path
    secret_file: Path
    pipeline_config: Path
    vault_path: Path
    status_file: Path
    vault_folder: str = "Omi/Conversations"
    host: str = "0.0.0.0"
    port: int = 7331
    poll_seconds: float = 2
    retry_seconds: float = 60
    max_attempts: int = 5
    job_timeout_seconds: float = 14400
    skip_vibe7: bool = False
    no_hermes: bool = False
    ffmpeg_dir: str = ""
    speaker_match_threshold: float = 0.40  # calibrated on Omi audio; see scripts/speaker_calibration.py
    speaker_match_margin: float = 0.12
    # Delete a recording's audio once its note is published (see docs/speakers.md, README).
    delete_audio_after_processing: bool = True
    # Mac meeting captures (docs/superpowers/specs/2026-09-29-meeting-capture-design.md §3).
    meetings_enabled: bool = False
    meetings_vault_folder: str = "Omi/Meetings"
    owner_name: str = "Me"  # who speaks on a meeting capture's mic channel

    @property
    def review_dir(self):
        return self.data_dir.parent / "review"

    @classmethod
    def load(cls, path: Path):
        raw = read_json(path)
        allowed = set(cls.__dataclass_fields__)
        if raw.keys() - allowed:
            raise ValueError("Unknown service configuration keys: " + ", ".join(sorted(raw.keys() - allowed)))
        for key in ["data_dir", "incoming_dir", "secret_file", "pipeline_config", "vault_path", "status_file"]:
            value = Path(os.path.expandvars(raw[key])).expanduser()
            if not value.is_absolute():
                raise ValueError(f"{key} must be an absolute path; services have no user working directory")
            raw[key] = value.resolve()
        cfg = cls(**raw)
        if not 0 < cfg.speaker_match_threshold <= 1 or not 0 < cfg.speaker_match_margin <= 1:
            raise ValueError("Invalid speaker matching threshold or margin")
        if cfg.ffmpeg_dir and not Path(cfg.ffmpeg_dir).is_absolute():
            raise ValueError("ffmpeg_dir must be an absolute path")
        if not 0 <= cfg.port <= 65535 or cfg.poll_seconds <= 0 or cfg.retry_seconds <= 0:
            raise ValueError("Invalid port or polling/retry interval")
        if cfg.max_attempts < 1 or cfg.job_timeout_seconds <= 0:
            raise ValueError("Invalid retry count or job timeout")
        check_folder("vault_folder", cfg.vault_folder)
        check_folder("meetings_vault_folder", cfg.meetings_vault_folder)
        owner = cfg.owner_name.strip()
        if not 1 <= len(owner) <= 80 or owner != cfg.owner_name or any(
            ord(c) < 32 or c in "[]<>\\|" for c in owner
        ):
            raise ValueError("owner_name must be 1–80 characters without control characters or markup brackets")
        return cfg

    def pipeline(self):
        result = read_json(self.pipeline_config)
        result["vault_path"] = str(self.vault_path)
        return result
