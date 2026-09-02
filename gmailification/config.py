"""Configuration loading and validation.

The config file is YAML. Secrets are never stored in it directly: each source
references an environment variable (``password_env``) or a mounted file
(``password_file``). See config/config.example.yaml.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import yaml


class ConfigError(Exception):
    pass


# Where an imported message lands in the destination Gmail:
#   inbox   -> INBOX + UNREAD (normal received mail; the default)
#   sent    -> Gmail's Sent view, already read, threaded, never in the inbox
#   archive -> label only (All Mail), already read
VALID_PLACES = ("inbox", "sent", "archive")


@dataclass(frozen=True)
class FolderConfig:
    name: str
    place: str = "inbox"
    label: str = ""  # optional override of the source label


@dataclass(frozen=True)
class ThrottleConfig:
    """Keeps gmailification a polite background citizen on a shared broadband line.

    bandwidth_limit_kbps: rough cap on transfer rate — after each message we
    sleep for size/limit seconds (0 = unlimited).
    max_messages_per_cycle: per source per cycle; the UID cursor persists, so
    anything beyond the cap is simply picked up next cycle (0 = unlimited).
    message_pause_seconds: fixed pause between messages (0 = none).

    Defined globally under `throttle:`; a source can override any subset in
    its own `throttle:` block (unset fields inherit the global values).
    """

    bandwidth_limit_kbps: int = 0
    max_messages_per_cycle: int = 200
    message_pause_seconds: float = 0.0


_THROTTLE_FIELDS = ("bandwidth_limit_kbps", "max_messages_per_cycle", "message_pause_seconds")


@dataclass(frozen=True)
class SourceConfig:
    user: str
    name: str
    host: str
    username: str
    password: str
    label: str
    port: int = 993
    folders: tuple[FolderConfig, ...] = (FolderConfig(name="INBOX"),)
    # Effective throttle for this source (global values merged with this
    # source's overrides); throttle_overrides names the overridden fields.
    throttle: ThrottleConfig = ThrottleConfig()
    throttle_overrides: tuple[str, ...] = ()
    # Effective poll interval (the global one unless this source overrides it).
    poll_interval_seconds: int = 180
    poll_interval_overridden: bool = False
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
    history_days: float = 14.0

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


def _parse_folder(context: str, raw) -> FolderConfig:
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        raise ConfigError(f"{context}: each folder must be a name or a mapping")
    name = str(_require(raw, "name", f"{context} folder")).strip()
    place = str(raw.get("place", "inbox")).strip().lower()
    if place not in VALID_PLACES:
        raise ConfigError(f"{context} folder {name!r}: place must be one of "
                          f"{', '.join(VALID_PLACES)}, got {place!r}")
    return FolderConfig(name=name, place=place, label=str(raw.get("label", "")))


def parse_folder_text(text: str) -> list:
    """Parse the compact UI syntax into raw YAML folder entries.

    One folder per line (commas also separate); optional modifiers:
        INBOX
        [Gmail]/Sent Mail :: sent
        Old/Archive :: archive :: Pulled/custom-label
    """
    entries: list = []
    for chunk in re.split(r"[\n,]+", text or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("::")]
        name = parts[0]
        place = (parts[1].lower() if len(parts) > 1 and parts[1] else "inbox")
        label = parts[2] if len(parts) > 2 else ""
        if place not in VALID_PLACES:
            raise ConfigError(f"folder {name!r}: place must be one of "
                              f"{', '.join(VALID_PLACES)}, got {place!r}")
        if place == "inbox" and not label:
            entries.append(name)
        else:
            entry: dict = {"name": name, "place": place}
            if label:
                entry["label"] = label
            entries.append(entry)
    return entries


def format_folder_text(folders: tuple[FolderConfig, ...]) -> str:
    """Inverse of parse_folder_text, for pre-filling the UI edit form."""
    lines = []
    for f in folders:
        line = f.name
        if f.place != "inbox" or f.label:
            line += f" :: {f.place}"
        if f.label:
            line += f" :: {f.label}"
        lines.append(line)
    return "\n".join(lines)


def _merge_throttle(base: ThrottleConfig, raw_t, context: str) -> tuple[ThrottleConfig, tuple[str, ...]]:
    """Merge a (possibly partial) throttle mapping onto base; returns the
    merged config plus which fields were overridden."""
    if not raw_t:
        return base, ()
    if not isinstance(raw_t, dict):
        raise ConfigError(f"{context}: throttle must be a mapping")
    unknown = set(raw_t) - set(_THROTTLE_FIELDS)
    if unknown:
        raise ConfigError(f"{context}: unknown throttle field(s): {', '.join(sorted(unknown))}")
    try:
        merged = ThrottleConfig(
            bandwidth_limit_kbps=int(raw_t.get("bandwidth_limit_kbps", base.bandwidth_limit_kbps)),
            max_messages_per_cycle=int(raw_t.get("max_messages_per_cycle", base.max_messages_per_cycle)),
            message_pause_seconds=float(raw_t.get("message_pause_seconds", base.message_pause_seconds)),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{context}: invalid throttle value: {exc}") from exc
    return merged, tuple(k for k in _THROTTLE_FIELDS if k in raw_t)


def _parse_source(user: str, raw: dict, default_throttle: ThrottleConfig,
                  default_poll_interval: int) -> SourceConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"user {user}: each source must be a mapping")
    name = str(_require(raw, "name", f"user {user} source"))
    context = f"source {user}/{name}"
    folders = raw.get("folders", ["INBOX"])
    if isinstance(folders, str):
        folders = [folders]
    if not folders:
        raise ConfigError(f"{context}: folders must not be empty")
    folder_cfgs = tuple(_parse_folder(context, f) for f in folders)
    folder_names = [f.name for f in folder_cfgs]
    if len(folder_names) != len(set(folder_names)):
        raise ConfigError(f"{context}: duplicate folder names")
    after_import = str(raw.get("after_import", "keep"))
    if after_import not in ("keep", "delete"):
        raise ConfigError(f"{context}: after_import must be 'keep' or 'delete', got {after_import!r}")
    throttle, overrides = _merge_throttle(default_throttle, raw.get("throttle"), context)
    interval_raw = raw.get("poll_interval_seconds")
    if interval_raw is None:
        poll_interval, interval_overridden = default_poll_interval, False
    else:
        try:
            poll_interval = int(interval_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{context}: invalid poll_interval_seconds: {interval_raw!r}") from exc
        if poll_interval < 10:
            raise ConfigError(f"{context}: poll_interval_seconds must be at least 10")
        interval_overridden = True
    return SourceConfig(
        user=user,
        name=name,
        host=str(_require(raw, "host", context)),
        port=int(raw.get("port", 993)),
        username=str(_require(raw, "username", context)),
        password=_resolve_password(user, name, raw),
        label=str(raw.get("label") or f"Pulled/{name}"),
        folders=folder_cfgs,
        backfill_days=int(raw.get("backfill_days", 0)),
        after_import=after_import,
        throttle=throttle,
        throttle_overrides=overrides,
        poll_interval_seconds=poll_interval,
        poll_interval_overridden=interval_overridden,
    )


def _parse_user(raw: dict, default_throttle: ThrottleConfig, default_poll_interval: int) -> UserConfig:
    if not isinstance(raw, dict):
        raise ConfigError("each user must be a mapping")
    name = str(_require(raw, "name", "user"))
    dest_raw = _require(raw, "destination", f"user {name}")
    dest = DestinationConfig(
        email=str(_require(dest_raw, "email", f"user {name} destination")),
        token_file=str(_require(dest_raw, "token_file", f"user {name} destination")),
    )
    sources_raw = raw.get("sources") or []
    sources = tuple(_parse_source(name, s, default_throttle, default_poll_interval)
                    for s in sources_raw)
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

    throttle, _ = _merge_throttle(ThrottleConfig(), raw.get("throttle"), path)
    poll_interval = int(raw.get("poll_interval_seconds", 180))

    # An empty user list is allowed: a fresh install starts with no users and
    # gets configured entirely through the web UI.
    users = tuple(_parse_user(u, throttle, poll_interval) for u in raw.get("users") or [])
    names = [u.name for u in users]
    if len(names) != len(set(names)):
        raise ConfigError(f"{path}: duplicate user names")
    emails = [u.destination.email for u in users]
    if len(emails) != len(set(emails)):
        raise ConfigError(f"{path}: two users share the same destination email")

    admin_raw = raw.get("admin") or {}
    admin_user = str(admin_raw.get("user", ""))
    if admin_user and admin_user not in names:
        raise ConfigError(f"{path}: admin.user {admin_user!r} is not a configured user")

    return AppConfig(
        users=users,
        throttle=throttle,
        poll_interval_seconds=poll_interval,
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
        history_days=float(raw.get("history_days", 14)),
    )
