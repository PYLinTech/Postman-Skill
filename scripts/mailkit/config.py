"""Server presets and credential resolution for the email skill.

Pure stdlib. No network access lives here so the resolution logic stays
unit-testable without a mailbox.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# (imap_host, imap_port, smtp_host, smtp_port)
MailServers = Tuple[str, int, str, int]

# Named providers. Ports follow each vendor's documented configuration:
#   993  = IMAP over implicit TLS
#   587  = SMTP submission over STARTTLS
#   465  = SMTP over implicit TLS
#   25   = plain SMTP (last resort)
_PROVIDER_PRESETS: Dict[str, MailServers] = {
    "qq": ("imap.qq.com", 993, "smtp.qq.com", 587),
    "gmail": ("imap.gmail.com", 993, "smtp.gmail.com", 587),
    "163": ("imap.163.com", 993, "smtp.163.com", 587),
    "126": ("imap.126.com", 993, "smtp.126.com", 587),
    "yeah": ("imap.yeah.net", 993, "smtp.yeah.net", 587),
    "outlook": ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "hotmail": ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "live": ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "yahoo": ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 587),
    "foxmail": ("imap.qq.com", 993, "smtp.qq.com", 587),
    "sohu": ("imap.sohu.com", 993, "smtp.sohu.com", 465),
    "sina": ("imap.sina.com", 993, "smtp.sina.com", 465),
    "aliyun": ("imap.aliyun.com", 993, "smtp.aliyun.com", 465),
    "tom": ("imap.tom.com", 993, "smtp.tom.com", 465),
}

# Domains that map onto a preset without the user naming a provider.
_DOMAIN_TO_PROVIDER: Dict[str, str] = {
    "qq.com": "qq",
    "foxmail.com": "foxmail",
    "gmail.com": "gmail",
    "googlemail.com": "gmail",
    "163.com": "163",
    "126.com": "126",
    "yeah.net": "yeah",
    "yahoo.com": "yahoo",
    "yahoo.com.cn": "yahoo",
    "ymail.com": "yahoo",
    "outlook.com": "outlook",
    "hotmail.com": "hotmail",
    "live.com": "live",
    "live.cn": "live",
    "msn.com": "live",
    "sohu.com": "sohu",
    "sina.com": "sina",
    "sina.cn": "sina",
    "aliyun.com": "aliyun",
    "tom.com": "tom",
}

DEFAULT_TIMEOUT = 30
DEFAULT_IMAP_HOST = "imap.qq.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_HOST = "smtp.qq.com"
DEFAULT_SMTP_PORT = 587

ENV_ADDRESS = "MAIL_ADDRESS"
ENV_PASSWORD = "MAIL_PASSWORD"
ENV_PROVIDER = "MAIL_PROVIDER"
ENV_CONFIG = "MAIL_CONFIG"
ENV_NAME = "MAIL_SENDER_NAME"

# RFC 2971 client identification. Generic and overridable via `client_name` so
# the skill carries no vendor branding. NetEase and QQ refuse LOGIN without it,
# so it is sent on every connect; servers that do not implement it simply reject
# the command, which callers treat as harmless.
IMAP_ID_NAME = "python-mailkit"
IMAP_ID_VERSION = "1.0.0"


def build_imap_id(name: str = IMAP_ID_NAME) -> str:
    """Return the argument string for the IMAP ``ID`` command."""
    return (f'("name" "{name}" "version" "{IMAP_ID_VERSION}" '
            f'"vendor" "{name}" "support-email" "noreply@localhost")')


class ConfigError(Exception):
    """Raised when credentials or server settings cannot be resolved."""


def _as_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def email_domain(address: str) -> str:
    text = str(address or "").strip().lower()
    return text.rsplit("@", 1)[-1] if "@" in text else ""


def smtp_security_for_port(port: int, override: Optional[str] = None) -> str:
    """Map an SMTP port onto a transport mode.

    465 is implicit TLS, 587 is STARTTLS, everything else is treated as plain.
    An explicit ``smtp_security`` always wins so odd corporate ports work.
    """
    if override:
        mode = str(override).strip().lower()
        if mode in {"ssl", "starttls", "none"}:
            return mode
    if port == 465:
        return "ssl"
    if port == 587:
        return "starttls"
    return "none"


def detect_provider(address: str, explicit: str = "") -> str:
    """Resolve a provider key from an explicit choice, then the email domain."""
    name = str(explicit or "").strip().lower()
    if name and name != "auto":
        return name
    return _DOMAIN_TO_PROVIDER.get(email_domain(address), "")


def resolve_servers(
    address: str,
    provider: str = "",
    overrides: Optional[Dict[str, Any]] = None,
) -> Tuple[MailServers, str]:
    """Return ``((imap_host, imap_port, smtp_host, smtp_port), provider_key)``.

    Precedence: explicit per-host overrides, then a named provider preset, then
    the email domain, then the QQ defaults.
    """
    overrides = overrides or {}
    key = detect_provider(address, provider)

    if key and key not in _PROVIDER_PRESETS:
        raise ConfigError(
            f"unknown mail provider {key!r}; use one of {sorted(_PROVIDER_PRESETS)} "
            "or 'auto'/'custom'"
        )

    preset = _PROVIDER_PRESETS.get(key)
    if preset is None:
        preset = (DEFAULT_IMAP_HOST, DEFAULT_IMAP_PORT, DEFAULT_SMTP_HOST, DEFAULT_SMTP_PORT)

    imap_host = str(overrides.get("imap_host") or "").strip() or preset[0]
    imap_port = _as_int(overrides.get("imap_port"), preset[1])
    smtp_host = str(overrides.get("smtp_host") or "").strip() or preset[2]
    smtp_port = _as_int(overrides.get("smtp_port"), preset[3])
    return (imap_host, imap_port, smtp_host, smtp_port), key


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must contain a JSON object")
    return data


def _pick(source: Dict[str, Any], key: str, env_name: str, default: str = "") -> str:
    value = source.get(key)
    if value not in (None, ""):
        return str(value).strip()
    return str(os.environ.get(env_name, default) or "").strip()


def load_config(
    config_path: str = "",
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve the effective mailbox settings.

    Precedence for every field: explicit override, then a JSON config file
    (argument > ``MAIL_CONFIG`` env), then environment variables.

    The password is required; everything else falls back to a preset. The
    returned dict is safe to serialise for diagnostics *except* ``password``,
    which :func:`redact` strips.
    """
    overrides = dict(overrides or {})

    path = str(overrides.get("config_path") or config_path or os.environ.get(ENV_CONFIG, "")).strip()
    file_data: Dict[str, Any] = {}
    if path:
        file_data = _read_json(Path(path).expanduser())
        # A file may hold several accounts under an "accounts" map; --account
        # picks one. An unknown name must fail loudly — falling through to the
        # file's top-level fields would quietly address the wrong mailbox.
        account = str(overrides.get("account") or "").strip()
        if account:
            accounts = file_data.get("accounts")
            if not isinstance(accounts, dict):
                raise ConfigError(
                    f"--account {account!r} was given but {path} has no "
                    '"accounts" map'
                )
            selected = accounts.get(account)
            if not isinstance(selected, dict):
                raise ConfigError(
                    f"account {account!r} not found in {path}; available: "
                    + (", ".join(sorted(accounts)) or "(none)")
                )
            file_data = {k: v for k, v in file_data.items() if k != "accounts"}
            file_data.update(selected)

    def field(key: str, env_name: str) -> str:
        return _pick(file_data, key, env_name) or _pick(overrides, key, "", "")

    address = field("address", ENV_ADDRESS)
    password = field("password", ENV_PASSWORD)
    if not address:
        raise ConfigError(
            "no mailbox address configured; set MAIL_ADDRESS, pass config_path, "
            "or copy resources/config.example.json"
        )
    if not password:
        raise ConfigError(
            "no mailbox password configured; set MAIL_PASSWORD or put it in the config file"
        )
    if "@" not in address:
        raise ConfigError(f"invalid mailbox address: {address!r}")

    provider = field("provider", ENV_PROVIDER)
    servers, key = resolve_servers(address, provider, overrides=file_data)

    imap_host = str(file_data.get("imap_host") or "").strip() or servers[0]
    imap_port = _as_int(file_data.get("imap_port"), servers[1])
    smtp_host = str(file_data.get("smtp_host") or "").strip() or servers[2]
    smtp_port = _as_int(file_data.get("smtp_port"), servers[3])
    # An explicit provider preset is only a default; a host the user pinned
    # always wins, even when it is a NetEase host the preset would not choose.
    imap_host = str(overrides.get("imap_host") or "").strip() or imap_host
    smtp_host = str(overrides.get("smtp_host") or "").strip() or smtp_host

    return {
        "address": address,
        "password": password,
        "sender_name": field("sender_name", ENV_NAME),
        "client_name": str(file_data.get("client_name") or "").strip() or IMAP_ID_NAME,
        "provider": key,
        "imap_host": imap_host,
        "imap_port": imap_port,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_security": str(file_data.get("smtp_security") or "").strip().lower(),
        "timeout": _as_int(file_data.get("timeout"), DEFAULT_TIMEOUT),
        "verify_ssl": bool(file_data.get("verify_ssl", True)),
        "send_as": str(file_data.get("send_as") or "").strip() or address,
    }


def redact(config: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of *config* with the password replaced by a fixed placeholder."""
    safe = dict(config)
    safe["password"] = "***" if config.get("password") else ""
    return safe
