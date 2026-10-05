"""IMAP session management.

Uses UID commands exclusively: sequence numbers shift whenever another client
touches the mailbox, while a UID is stable for the life of the message. Search
uses ``BODY.PEEK`` so listing mail never flips the read flag as a side effect.
"""

from __future__ import annotations

import imaplib
import re
import ssl
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import IMAP_ID_NAME, ConfigError, build_imap_id
from .mime import MailError, parse_headers_only

# RFC 2971 client identification; imaplib does not register the command.
imaplib.Commands.setdefault("ID", ("AUTH", "NONAUTH", "SELECTED"))

_DEFAULT_FOLDER = "INBOX"
_FOLDER_ALIASES = {
    "inbox": "INBOX",
    "sent": "Sent",
    "sent items": "Sent",
    "draft": "Drafts",
    "drafts": "Drafts",
    "trash": "Trash",
    "deleted": "Trash",
    "junk": "Junk",
    "spam": "Junk",
    "archive": "Archive",
    "all": "",
}

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def _quote_folder(name: str) -> str:
    """Quote a folder name for IMAP, escaping backslashes and quotes."""
    return '"' + str(name).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _check(typ: str, data: Any, action: str) -> Any:
    if typ != "OK":
        detail = ""
        if isinstance(data, list) and data:
            raw = data[0]
            detail = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        raise MailError(f"{action} failed ({typ}): {detail}".strip())
    return data


def _normalise_date(value: str) -> str:
    """Convert ``YYYY-MM-DD`` to IMAP's ``01-Jan-2024``; pass others through."""
    match = _DATE_RE.match(str(value or "").strip())
    if not match:
        return str(value or "").strip()
    year, month, day = match.groups()
    if not 1 <= int(month) <= 12:
        raise MailError(f"invalid month in date: {value!r}")
    return f"{int(day):02d}-{_MONTHS[int(month) - 1]}-{year}"


def normalise_folder(name: str) -> str:
    """Map a friendly alias onto a real folder name; ``''`` means all folders."""
    raw = str(name or "").strip()
    if not raw:
        return _DEFAULT_FOLDER
    return _FOLDER_ALIASES.get(raw.lower(), raw)


def _special_key(name: str) -> str:
    """Normalise for special-use matching: ``Drafts`` and ``\\Draft`` agree."""
    key = str(name or "").strip().lstrip("\\").lower()
    return key[:-1] if key.endswith("s") else key


class ImapSession:
    """A logged-in IMAP session. Use as a context manager."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self._config = config
        self._imap: Optional[imaplib.IMAP4_SSL] = None
        self._folder: str = _DEFAULT_FOLDER

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> "ImapSession":
        self.connect()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def connect(self) -> "ImapSession":
        host = self._config["imap_host"]
        port = int(self._config["imap_port"])
        timeout = int(self._config.get("timeout", 30))
        context = ssl.create_default_context()
        if not self._config.get("verify_ssl", True):
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        try:
            self._imap = imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=timeout)
        except ssl.SSLCertVerificationError as exc:
            raise ConfigError(
                f"TLS certificate verification failed for {host}:{exc}. "
                "Use a valid certificate, or set verify_ssl=false in the config "
                "if you accept the risk."
            ) from exc
        except OSError as exc:
            raise MailError(f"cannot connect to {host}:{port}: {exc}") from exc
        self._send_id()
        try:
            self._imap.login(self._config["address"], self._config["password"])
        except imaplib.IMAP4.error as exc:
            raise MailError(
                f"IMAP login failed for {self._config['address']} at {host}:{port}. "
                "Check the address and password — most providers require an "
                "app-specific password rather than the account password. "
                f"({exc})"
            ) from exc
        return self

    def _send_id(self) -> None:
        """Send the RFC 2971 client ID; NetEase and QQ reject LOGIN without it."""
        assert self._imap is not None
        args = build_imap_id(str(self._config.get("client_name") or IMAP_ID_NAME))
        try:
            typ, _ = self._imap._simple_command("ID", args)  # noqa: SLF001
            if typ != "OK":
                raise MailError(f"IMAP ID failed: {typ}")
        except imaplib.IMAP4.error:
            # Servers that do not advertise ID reject the command; that is fine.
            pass

    def close(self) -> None:
        if self._imap is not None:
            try:
                self._imap.close()
            except Exception:
                pass
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None

    @property
    def imap(self) -> imaplib.IMAP4_SSL:
        if self._imap is None:
            raise MailError("IMAP session is not connected")
        return self._imap

    # -- capabilities ----------------------------------------------------
    def capabilities(self) -> Tuple[str, ...]:
        return tuple(str(c).upper() for c in (self.imap.capabilities or ()))

    def supports_gmail_raw(self) -> bool:
        return "X-GM-RAW" in self.capabilities()

    def _is_gmail(self) -> bool:
        return "imap.gmail.com" in str(self._config.get("imap_host", "")).lower()

    # -- folders ---------------------------------------------------------
    def list_folders(self) -> List[Dict[str, Any]]:
        typ, data = self.imap.list()
        _check(typ, data, "LIST")
        out: List[Dict[str, Any]] = []
        for raw in data or []:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            match = re.match(r'\((?P<flags>[^)]*)\)\s+"?(?P<delim>[^" ]+)\s+(?P<name>.+?)"?$', line)
            if not match:
                continue
            name = match.group("name").strip().strip('"')
            flags = match.group("flags").lower().split()
            out.append({
                "name": name,
                "special": next((f.lstrip("\\") for f in flags if f.startswith("\\")), ""),
                "delimiter": match.group("delim"),
            })
        return out

    def find_folder(self, wanted: str) -> str:
        """Resolve an alias to a real folder, tolerating localised names."""
        target = normalise_folder(wanted)
        if target == _DEFAULT_FOLDER:
            return target
        folders = {f["name"]: f for f in self.list_folders()}
        if target in folders:
            return target
        # Localised mailboxes (Sent Items, 已发送, Gesendet, ...) differ per UI
        # language; fall back to the standard special-use flag, ignoring a
        # trailing plural on either side so Drafts matches \Drafts.
        wanted_key = _special_key(target)
        for name, meta in folders.items():
            if meta["special"] and _special_key(meta["special"]) == wanted_key:
                return name
        lowered = {name.lower(): name for name in folders}
        if target.lower() in lowered:
            return lowered[target.lower()]
        raise MailError(
            f"folder {target!r} not found; available: " + ", ".join(sorted(folders)[:20])
        )

    def select(self, folder: str = "") -> str:
        name = self.find_folder(folder or _DEFAULT_FOLDER)
        typ, data = self.imap.select(_quote_folder(name), readonly=False)
        _check(typ, data, f"SELECT {name}")
        self._folder = name
        return name

    @property
    def current_folder(self) -> str:
        return self._folder

    # -- searching -------------------------------------------------------
    def search(
        self,
        query: str = "ALL",
        *,
        since: str = "",
        before: str = "",
        unread_only: bool = False,
        flagged_only: bool = False,
        has_attachment: bool = False,
        gmail_raw: str = "",
        limit: int = 0,
    ) -> List[str]:
        """Return UIDs, newest last. ``gmail_raw`` uses Gmail's native syntax."""
        criteria: List[str] = []
        if gmail_raw and (self.supports_gmail_raw() or self._is_gmail()):
            typ, data = self.imap.uid("search", None, "X-GM-RAW", gmail_raw)
            _check(typ, data, "UID SEARCH (X-GM-RAW)")
            uids = (data[0] or b"").split()
        else:
            if query:
                criteria.append(str(query).strip() or "ALL")
            if since:
                criteria.append(f'"SINCE" "{_normalise_date(since)}"')
            if before:
                criteria.append(f'"BEFORE" "{_normalise_date(before)}"')
            if unread_only:
                criteria.append("UNSEEN")
            if flagged_only:
                criteria.append("FLAGGED")
            if has_attachment:
                criteria.append('HEADER Content-Type "multipart/mixed"')
            typ, data = self.imap.uid("search", *criteria)
            _check(typ, data, "UID SEARCH")
            uids = (data[0] or b"").split()

        out = [u.decode() for u in uids if u]
        return out[-limit:] if limit and limit > 0 else out

    def count_unseen(self) -> int:
        try:
            typ, data = self.imap.status(_quote_folder(self._folder), "(UNSEEN)")
            _check(typ, data, "STATUS")
            raw = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
            match = re.search(r"UNSEEN\s+(\d+)", raw)
            return int(match.group(1)) if match else 0
        except Exception:
            return 0

    # -- fetching --------------------------------------------------------
    def fetch_headers(self, uids: Sequence[str]) -> List[Dict[str, Any]]:
        if not uids:
            return []
        typ, data = self.imap.uid(
            "fetch", ",".join(uids), "(BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)])"
        )
        _check(typ, data, "UID FETCH headers")
        return self._collect(data, headers_only=True)

    def fetch_messages(
        self,
        uids: Sequence[str],
        *,
        include_html: bool = False,
        max_body_chars: int = 0,
    ) -> List[Dict[str, Any]]:
        if not uids:
            return []
        typ, data = self.imap.uid("fetch", ",".join(uids), "(BODY.PEEK[])")
        _check(typ, data, "UID FETCH body")
        from .mime import parse_message  # local import keeps module import cheap

        out: List[Dict[str, Any]] = []
        for uid, raw in self._raw_chunks(data):
            item = parse_message(raw, include_html=include_html,
                                 max_body_chars=max_body_chars)
            item["uid"] = uid
            out.append(item)
        return out

    def fetch_raw(self, uid: str) -> bytes:
        """Fetch one message's full RFC 822 bytes without marking it read."""
        typ, data = self.imap.uid("fetch", str(uid), "(BODY.PEEK[])")
        _check(typ, data, f"UID FETCH {uid}")
        for _found_uid, raw in self._raw_chunks(data):
            return raw
        raise MailError(f"message uid {uid} not found or has no body")

    def _raw_chunks(self, data: Any) -> Iterable[Tuple[str, bytes]]:
        for chunk in data or []:
            if not isinstance(chunk, tuple) or len(chunk) < 2:
                continue
            meta, payload = chunk[0], chunk[1]
            uid = ""
            if isinstance(meta, bytes):
                head = meta.decode("utf-8", "replace")
                match = re.search(r"UID\s+(\d+)", head)
                if match:
                    uid = match.group(1)
            if isinstance(payload, bytes) and payload:
                yield uid, payload

    def _collect(self, data: Any, *, headers_only: bool) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for uid, raw in self._raw_chunks(data):
            item = parse_headers_only(raw) if headers_only else {}
            item["uid"] = uid
            out.append(item)
        return out

    # -- flags -----------------------------------------------------------
    def set_flags(self, uids: Sequence[str], flags: Sequence[str], *, mode: str = "set") -> Dict[str, Any]:
        if not uids:
            return {"updated": 0, "requested": list(flags), "mode": mode}
        typed = tuple(flags)
        if mode == "add":
            typ, data = self.imap.uid("store", ",".join(uids), "+FLAGS.SILENT", typed)
        elif mode == "remove":
            typ, data = self.imap.uid("store", ",".join(uids), "-FLAGS.SILENT", typed)
        else:
            typ, data = self.imap.uid("store", ",".join(uids), "FLAGS.SILENT", typed)
        _check(typ, data, "UID STORE")
        return {"updated": len(uids), "requested": list(flags), "mode": mode}

    def copy(self, uids: Sequence[str], destination: str) -> str:
        target = self.find_folder(destination)
        typ, data = self.imap.uid("copy", ",".join(uids), _quote_folder(target))
        _check(typ, data, f"UID COPY to {target}")
        return target

    def move(self, uids: Sequence[str], destination: str) -> str:
        """Move via COPY + ``\\Deleted`` + EXPUNGE, which every server supports.

        EXPUNGE is not UID-scoped, so anything else already flagged ``\\Deleted``
        in this folder goes too. That matches every other IMAP client.
        """
        target = self.copy(uids, destination)
        self.set_flags(uids, ["\\Deleted"], mode="add")
        self.expunge()
        return target

    def expunge(self) -> None:
        typ, data = self.imap.expunge()
        _check(typ, data, "EXPUNGE")

    def append(self, folder: str, message: Any) -> str:
        target = self.find_folder(folder)
        raw = message.as_bytes()
        typ, data = self.imap.append(_quote_folder(target), None, None, raw)
        _check(typ, data, f"APPEND to {target}")
        return target

    def quota_summary(self) -> Dict[str, Any]:
        """Best-effort mailbox usage; not all servers implement QUOTA."""
        try:
            typ, data = self.imap.getquota(_quote_folder(""))
            if typ != "OK" or not data or not isinstance(data[0], bytes):
                return {}
            parts = data[0].decode("utf-8", "replace").split()
            out: Dict[str, Any] = {}
            for index in range(0, len(parts) - 1, 2):
                out[parts[index]] = int(parts[index + 1])
            return out
        except Exception:
            return {}
