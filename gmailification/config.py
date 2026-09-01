"""Configuration loading and validation.

The config file is YAML. Secrets are never stored in it directly: each source
references an environment variable (``password_env``) or a mounted file
(``password_file``). See config/config.example.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class SourceConfig:
    user: str
    name: str
    host: str
    username: str
    password: str
    label: str
    port: int = 993
    folders: tuple[str, ...] = ("INBOX",)
    # On the very first run for a folder, how many days of existing mail to
    # backfill. 0 (default) = only mail that arrives after gmailification starts.
    backfill_days: int = 0
    # "keep" (default): copy semantics — the source mailbox is never modified.
    # "delete": move semantics — a message is expunged from the source, but
    # only after its Gmail import succeeded and was recorded.
    after_import: str = "keep"

    @property
    def key(self) -> str:
        return f"{self.user}/{self.name}"


@dataclass(frozen=True)
class DestinationConfig:
    email: str
    token_file: str


@dataclass(frozen=True)
class UserConfig:
    name: str
    destination: DestinationConfig
    sources: tuple[SourceConfig, ...]

    @property
    def destination_key(self) -> str:
        # Pseudo source_key used to track destination (OAuth) health.
        return f"{self.name}/_destination"


@dataclass(frozen=True)
class ThrottleConfig:
    """Keeps gmailification a polite background citizen on a shared broadband line.

    bandwidth_limit_kbps: rough cap on transfer rate — after each message we
    sleep for size/limit seconds (0 = unlimited).
    max_messages_per_cycle: per source per cycle; the UID cursor persists, so
    anything beyond the cap is simply picked up next cycle (0 = unlimited).
    message_pause_seconds: fixed pause between messages (0 = none).
    """

    bandwidth_limit_kbps: int = 0
    max_messages_per_cycle: int = 200
    message_pause_seconds: float = 0.0


@dataclass(frozen=True)
class AppConfig:
    users: tuple[UserConfig, ...]
    throttle: ThrottleConfig = field(default_factory=ThrottleConfig)
    poll_interval_seconds: int = 180
    alert_after_hours: float = 6.0
    realert_after_hours: float = 24.0
    http_bind: str = "0.0.0.0"
    http_port: int = 8377
    oauth_client_file: str = "/config/client_secret.json"
    db_path: str = "/data/gmailification.db"
    heartbeat_file: str = "/data/heartbeat"
    secrets_dir: str = "/data/secrets"
    admin_user: str = ""
    admin_copy_alerts: bool = True

    def user(self, name: str) -> UserConfig:
        for u in self.users:
            if u.name == name:
                return u
        raise ConfigError(f"unknown user {name!r}")

    @property
    def admin(self) -> UserConfig | None:
        if not self.admin_user:
            return None
        return self.user(self.admin_user)


def _resolve_password(user: str, name: str, raw: dict) -> str:
    env = raw.get("password_env")
    pw_file = raw.get("password_file")
    literal = raw.get("password")
    given = [k for k, v in (("password_env", env), ("password_file", pw_file), ("password", literal)) if v]
    if len(given) != 1:
        raise ConfigError(
            f"source {user}/{name}: exactly one of password_env, password_file or "
            f"password is required (got: {given or 'none'})"
        )
    if env:
        value = os.environ.get(env)
        if not value:
            raise ConfigError(f"source {user}/{name}: environment variable {env} is not set")
        return value
    if pw_file:
        try:
            with open(pw_file, encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError as exc:
            raise ConfigError(f"source {user}/{name}: cannot read password_file {pw_file}: {exc}") from exc
        if not value:
            raise ConfigError(f"source {user}/{name}: password_file {pw_file} is empty")
        return value
    return str(literal)


def _require(raw: dict, key: str, context: str):
    if key not in raw or raw[key] in (None, ""):
        raise ConfigError(f"{context}: missing required field {key!r}")
    return raw[key]


def _parse_source(user: str, raw: dict) -> SourceConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"user {user}: each source must be a mapping")
    name = str(_require(raw, "name", f"user {user} source"))
    context = f"source {user}/{name}"
    folders = raw.get("folders", ["INBOX"])
    if isinstance(folders, str):
        folders = [folders]
    if not folders:
        raise ConfigError(f"{context}: folders must not be empty")
    after_import = str(raw.get("after_import", "keep"))
    if after_import not in ("keep", "delete"):
        raise ConfigError(f"{context}: after_import must be 'keep' or 'delete', got {after_import!r}")
    return SourceConfig(
        user=user,
        name=name,
        host=str(_require(raw, "host", context)),
        port=int(raw.get("port", 993)),
        username=str(_require(raw, "username", context)),
        password=_resolve_password(user, name, raw),
        label=str(raw.get("label") or f"Pulled/{name}"),
        folders=tuple(str(f) for f in folders),
        backfill_days=int(raw.get("backfill_days", 0)),
        after_import=after_import,
    )


def _parse_user(raw: dict) -> UserConfig:
    if not isinstance(raw, dict):
        raise ConfigError("each user must be a mapping")
    name = str(_require(raw, "name", "user"))
    dest_raw = _require(raw, "destination", f"user {name}")
    dest = DestinationConfig(
        email=str(_require(dest_raw, "email", f"user {name} destination")),
        token_file=str(_require(dest_raw, "token_file", f"user {name} destination")),
    )
    sources_raw = raw.get("sources") or []
    sources = tuple(_parse_source(name, s) for s in sources_raw)
    seen = set()
    for s in sources:
        if s.name in seen:
            raise ConfigError(f"user {name}: duplicate source name {s.name!r}")
        seen.add(s.name)
    return UserConfig(name=name, destination=dest, sources=sources)


def load_config(path: str) -> AppConfig:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    # An empty user list is allowed: a fresh install starts with no users and
    # gets configured entirely through the web UI.
    users = tuple(_parse_user(u) for u in raw.get("users") or [])
    names = [u.name for u in users]
    if len(names) != len(set(names)):
        raise ConfigError(f"{path}: duplicate user names")
    emails = [u.destination.email for u in users]
    if len(emails) != len(set(emails)):
        raise ConfigError(f"{path}: two users share the same destination email")

    throttle_raw = raw.get("throttle") or {}
    throttle = ThrottleConfig(
        bandwidth_limit_kbps=int(throttle_raw.get("bandwidth_limit_kbps", 0)),
        max_messages_per_cycle=int(throttle_raw.get("max_messages_per_cycle", 200)),
        message_pause_seconds=float(throttle_raw.get("message_pause_seconds", 0)),
    )

    admin_raw = raw.get("admin") or {}
    admin_user = str(admin_raw.get("user", ""))
    if admin_user and admin_user not in names:
        raise ConfigError(f"{path}: admin.user {admin_user!r} is not a configured user")

    return AppConfig(
        users=users,
        throttle=throttle,
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 180)),
        alert_after_hours=float(raw.get("alert_after_hours", 6)),
        realert_after_hours=float(raw.get("realert_after_hours", 24)),
        http_bind=str(raw.get("http_bind", "0.0.0.0")),
        http_port=int(raw.get("http_port", 8377)),
        oauth_client_file=str(raw.get("oauth_client_file", "/config/client_secret.json")),
        db_path=str(raw.get("db_path", "/data/gmailification.db")),
        heartbeat_file=str(raw.get("heartbeat_file", "/data/heartbeat")),
        secrets_dir=str(raw.get("secrets_dir", "/data/secrets")),
        admin_user=admin_user,
        admin_copy_alerts=bool(admin_raw.get("copy_alerts", True)),
    )
