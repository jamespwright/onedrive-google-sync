import os
import re
from dataclasses import dataclass

import yaml

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.yaml")
TOKEN_DIR = os.path.join(CONFIG_DIR, "tokens")
STATE_PATH = os.path.join(CONFIG_DIR, "state.db")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ConfigError(ValueError):
    pass


@dataclass
class Source:
    onedrive_account: str
    album: str | None = None
    album_id: str | None = None

    @property
    def label(self) -> str:
        return f"{self.onedrive_account}/{self.album or self.album_id}"


@dataclass
class Target:
    google_account: str
    google_album: str
    sources: list[Source]


@dataclass
class Config:
    schedule_enabled: bool
    schedule_time: str  # "HH:MM", daily, in the container's local time (TZ)
    include_videos: bool
    allow_empty_source: bool
    onedrive_client_id: str
    onedrive_scopes: list[str]
    google_client_secret_file: str
    targets: list[Target]


def read_raw(path: str = CONFIG_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_raw(raw: dict, path: str = CONFIG_PATH):
    parse(raw, require_targets=False)  # refuse to write a config that won't load
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def load(path: str = CONFIG_PATH, require_targets: bool = True) -> Config:
    if not os.path.exists(path):
        raise ConfigError(f"No config at {path}. Copy config.example.yaml there and edit it.")
    return parse(read_raw(path), require_targets)


def parse(raw: dict, require_targets: bool = True) -> Config:
    onedrive = raw.get("onedrive") or {}
    google = raw.get("google") or {}
    schedule = raw.get("schedule") or {}
    if not onedrive.get("client_id"):
        raise ConfigError("onedrive.client_id is required")
    schedule_time = str(schedule.get("time", "03:00"))
    if not TIME_RE.match(schedule_time):
        raise ConfigError(f"schedule.time must be HH:MM, not {schedule_time!r}")

    targets = []
    for i, t in enumerate(raw.get("targets") or []):
        if not t.get("google_album"):
            raise ConfigError(f"targets[{i}].google_album is required")
        sources = []
        for j, s in enumerate(t.get("sources") or []):
            if not s.get("onedrive_account") or not (s.get("album") or s.get("album_id")):
                raise ConfigError(f"targets[{i}].sources[{j}] needs onedrive_account and album (or album_id)")
            sources.append(Source(s["onedrive_account"], s.get("album"), s.get("album_id")))
        if not sources:
            raise ConfigError(f"targets[{i}] has no sources")
        targets.append(Target(t.get("google_account", "default"), t["google_album"], sources))
    if require_targets and not targets:
        raise ConfigError("No targets configured")

    secret = google.get("client_secret_file", "google_client_secret.json")
    return Config(
        schedule_enabled=bool(schedule.get("enabled", True)),
        schedule_time=schedule_time,
        include_videos=bool(raw.get("include_videos", False)),
        allow_empty_source=bool(raw.get("allow_empty_source", False)),
        onedrive_client_id=onedrive["client_id"],
        onedrive_scopes=onedrive.get("scopes") or ["Files.Read"],
        google_client_secret_file=secret if os.path.isabs(secret) else os.path.join(CONFIG_DIR, secret),
        targets=targets,
    )
