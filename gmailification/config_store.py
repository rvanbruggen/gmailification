"""Read/mutate/write the YAML config file safely (used by the web UI).

All mutations operate on the *raw* YAML structure, are validated by running
the full config parser on the result, and are written atomically (with a .bak
of the previous version). Passwords entered through the UI are never stored in
the YAML: they are written as mode-0600 files under secrets_dir and referenced
via password_file.

Note: saving through the UI rewrites the YAML file, so hand-written comments
in config.yaml do not survive a UI edit.
"""

from __future__ import annotations

import os
import tempfile
import threading

import yaml

from .config import AppConfig, ConfigError, load_config, parse_folder_text

_FORBIDDEN_NAME_CHARS = set("/\\ \t\n\"'")


def _check_name(name: str, what: str) -> str:
    name = (name or "").strip()
    if not name or any(c in _FORBIDDEN_NAME_CHARS for c in name):
        raise ConfigError(f"invalid {what} name {name!r}: must be non-empty, "
                          "without spaces, quotes or slashes")
    return name


class ConfigStore:
    def __init__(self, path: str, secrets_dir: str):
        self._path = path
        self._secrets_dir = secrets_dir
        self._lock = threading.Lock()

    # -- raw io ------------------------------------------------------------

    def read_raw(self) -> dict:
        with open(self._path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if not isinstance(raw, dict):
            raise ConfigError(f"{self._path}: top level must be a mapping")
        return raw

    def validate_raw(self, raw: dict) -> AppConfig:
        """Run the full parser over a candidate config without touching disk."""
        fd, tmp = tempfile.mkstemp(suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True)
            return load_config(tmp)
        finally:
            os.unlink(tmp)

    def write_raw(self, raw: dict) -> AppConfig:
        """Validate, back up the current file, and atomically replace it."""
        with self._lock:
            cfg = self.validate_raw(raw)
            if os.path.exists(self._path):
                with open(self._path, "rb") as fh:
                    old = fh.read()
                with open(self._path + ".bak", "wb") as fh:
                    fh.write(old)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True)
            os.replace(tmp, self._path)
            return cfg

    # -- secrets -----------------------------------------------------------

    def store_password(self, user: str, source: str, password: str) -> str:
        """Write a UI-entered password to a 0600 file; returns its path."""
        os.makedirs(self._secrets_dir, mode=0o700, exist_ok=True)
        path = os.path.join(self._secrets_dir, f"{user}__{source}.pw")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(password)
        return path

    # -- mutations (pure raw-dict edits; call write_raw afterwards) --------

    @staticmethod
    def _users(raw: dict) -> list:
        return raw.setdefault("users", [])

    @staticmethod
    def _find_user(raw: dict, name: str) -> dict:
        for u in ConfigStore._users(raw):
            if isinstance(u, dict) and u.get("name") == name:
                return u
        raise ConfigError(f"unknown user {name!r}")

    @staticmethod
    def _find_source(user_raw: dict, name: str) -> dict:
        for s in user_raw.get("sources") or []:
            if isinstance(s, dict) and s.get("name") == name:
                return s
        raise ConfigError(f"unknown source {name!r}")

    def update_globals(self, raw: dict, values: dict) -> None:
        """values: subset of the global scalar settings + throttle/admin blocks."""
        for key in ("poll_interval_seconds", "alert_after_hours", "realert_after_hours"):
            if key in values and values[key] != "":
                raw[key] = float(values[key]) if "hours" in key else int(values[key])
        throttle = raw.setdefault("throttle", {})
        for key in ("bandwidth_limit_kbps", "max_messages_per_cycle"):
            if key in values and values[key] != "":
                throttle[key] = int(values[key])
        if values.get("message_pause_seconds", "") != "":
            throttle["message_pause_seconds"] = float(values["message_pause_seconds"])
        if values.get("admin_user", "") != "":
            raw.setdefault("admin", {})["user"] = str(values["admin_user"])
        if "admin_copy_alerts" in values:
            raw.setdefault("admin", {})["copy_alerts"] = bool(values["admin_copy_alerts"])

    def add_user(self, raw: dict, name: str, email: str) -> None:
        name = _check_name(name, "user")
        email = (email or "").strip()
        if "@" not in email:
            raise ConfigError(f"invalid destination email {email!r}")
        if any(u.get("name") == name for u in self._users(raw) if isinstance(u, dict)):
            raise ConfigError(f"user {name!r} already exists")
        self._users(raw).append({
            "name": name,
            "destination": {
                "email": email,
                "token_file": f"/data/tokens/{name}.json",
            },
            "sources": [],
        })

    def delete_user(self, raw: dict, name: str) -> None:
        user = self._find_user(raw, name)
        self._users(raw).remove(user)
        admin = raw.get("admin") or {}
        if admin.get("user") == name:
            admin.pop("user", None)

    def upsert_source(self, raw: dict, user: str, values: dict) -> None:
        """Create or update a source from UI form values.

        values keys: name, host, port, username, label, folders (text: one
        folder per line or comma-separated, each "name [:: place [:: label]]"),
        backfill_days, after_import, and exactly one credential input:
        password (plaintext, stored as a secret file) or password_env.
        """
        user_raw = self._find_user(raw, user)
        name = _check_name(values.get("name", ""), "source")
        sources = user_raw.setdefault("sources", [])
        existing = next((s for s in sources if isinstance(s, dict) and s.get("name") == name), None)

        entry = dict(existing) if existing else {"name": name}
        for key in ("host", "username", "label"):
            if values.get(key, "").strip():
                entry[key] = values[key].strip()
        if values.get("port", "").strip():
            entry["port"] = int(values["port"])
        if values.get("folders", "").strip():
            entry["folders"] = parse_folder_text(values["folders"])
        if values.get("backfill_days", "").strip():
            entry["backfill_days"] = int(values["backfill_days"])
        if values.get("after_import", "").strip():
            entry["after_import"] = values["after_import"].strip()

        throttle_fields = (
            ("throttle_bandwidth_limit_kbps", "bandwidth_limit_kbps", int),
            ("throttle_max_messages_per_cycle", "max_messages_per_cycle", int),
            ("throttle_message_pause_seconds", "message_pause_seconds", float),
        )
        if any(form_key in values for form_key, _, _ in throttle_fields):
            overrides: dict = {}
            for form_key, cfg_key, conv in throttle_fields:
                text = values.get(form_key, "").strip()
                if text:
                    try:
                        overrides[cfg_key] = conv(text)
                    except ValueError as exc:
                        raise ConfigError(f"invalid throttle value for {cfg_key}: {text!r}") from exc
            if overrides:
                entry["throttle"] = overrides
            else:
                # All fields left blank = revert to the global throttle.
                entry.pop("throttle", None)

        password = values.get("password", "")
        password_env = values.get("password_env", "").strip()
        if password and password_env:
            raise ConfigError("provide either a password or a password_env, not both")
        if password:
            entry["password_file"] = self.store_password(user, name, password)
            entry.pop("password_env", None)
            entry.pop("password", None)
        elif password_env:
            entry["password_env"] = password_env
            entry.pop("password_file", None)
            entry.pop("password", None)
        elif existing is None:
            raise ConfigError("a new source needs a password (or a password_env)")

        if existing is None:
            sources.append(entry)
        else:
            sources[sources.index(existing)] = entry

    def delete_source(self, raw: dict, user: str, name: str) -> None:
        user_raw = self._find_user(raw, user)
        source = self._find_source(user_raw, name)
        user_raw["sources"].remove(source)
