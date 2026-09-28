from __future__ import annotations
from dataclasses import dataclass
import json
import os
from pathlib import Path


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


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
        if cfg.ffmpeg_dir and not Path(cfg.ffmpeg_dir).is_absolute():
            raise ValueError("ffmpeg_dir must be an absolute path")
        if not 0 <= cfg.port <= 65535 or cfg.poll_seconds <= 0 or cfg.retry_seconds <= 0:
            raise ValueError("Invalid port or polling/retry interval")
        if cfg.max_attempts < 1 or cfg.job_timeout_seconds <= 0:
            raise ValueError("Invalid retry count or job timeout")
        folder = cfg.vault_folder.replace("\\", "/")
        if (
            not folder
            or Path(folder).is_absolute()
            or any(x in ("..", ".", "") for x in folder.split("/"))
            or ":" in folder
        ):
            raise ValueError("vault_folder must be a safe relative folder")
        return cfg

    def pipeline(self):
        result = read_json(self.pipeline_config)
        result["vault_path"] = str(self.vault_path)
        return result
