"""Command implementations.

Every function takes the parsed argument dict and returns a JSON-serialisable
result. The password never appears in a return value or an error message.
"""

from __future__ import annotations

import contextlib
import email
import re
from pathlib import Path
from typing import Any, Dict, List

from .config import load_config, redact
from .imap_client import ImapSession
from .mime import (
    MailError,
    build_message,
    decode_header_value,
    safe_filename,
    summarize_text,
    unique_path,
)
from .smtp_client import send_message as smtp_send

_SEEN = "\\Seen"
# read/unread name a desired *state*, so they invert \Seen rather than toggling
# it: asking for "unread" must clear the flag, whatever --mode says.
_STATE_FLAGS = {"read": (_SEEN, True), "seen": (_SEEN, True), "unread": (_SEEN, False)}
_FLAG_ALIASES = {
    "star": "\\Flagged",
    "starred": "\\Flagged",
    "flagged": "\\Flagged",
    "answered": "\\Answered",
    "deleted": "\\Deleted",
    "draft": "\\Draft",
    "forwarded": "$Forwarded",
    "recent": "\\Recent",
}


def _as_list(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in re.split(r"[;,]", str(value)) if part.strip()]


def _dedupe(items: List[str]) -> List[str]:
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _plan_flag_changes(flags: List[str], mode: str) -> tuple:
    """Return ``(add, remove)`` IMAP flags for a request.

    Later names win: asking for ``read unread`` leaves the message unread,
    and a flag never lands on both lists.
    """
    add: List[str] = []
    remove: List[str] = []
    for raw in flags:
        key = str(raw).strip().lower().lstrip("-")
        if key in _STATE_FLAGS:
            flag, want = _STATE_FLAGS[key]
            if mode == "remove":
                want = not want
        else:
            flag = _FLAG_ALIASES.get(key, str(raw).strip())
            want = mode != "remove"
        if want:
            remove = [f for f in remove if f != flag]
            add.append(flag)
        else:
            add = [f for f in add if f != flag]
            remove.append(flag)
    return _dedupe(add), _dedupe(remove)


def _resolve_uids(args: Dict[str, Any], session: ImapSession) -> List[str]:
    uids = [str(u).strip() for u in _as_list(args.get("uid")) if str(u).strip()]
    if uids:
        return uids
    # Falling back to a query keeps ad-hoc usage possible without a prior search.
    if args.get("query") or args.get("since"):
        found = session.search(
            str(args.get("query") or "ALL"),
            since=str(args.get("since") or ""),
            limit=int(args.get("limit") or 1),
        )
        if found:
            return found
    raise MailError("no uid given and no query to fall back on")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

@contextlib.contextmanager
def _mailbox(args: Dict[str, Any], *, select: str = ""):
    """Yield ``(config, session)`` with a live connection and *select* applied.

    Centralises the resolve-credentials / connect / select / close sequence so
    every command shares one session lifecycle and one error surface.
    """
    config = load_config(args.get("config_path", ""), args)
    session = ImapSession(config)
    try:
        session.connect()
        if select:
            session.select(select)
        yield config, session
    finally:
        session.close()


def _target_folder(args: Dict[str, Any]) -> str:
    return str(args.get("folder") or "INBOX")


def cmd_check(args: Dict[str, Any]) -> Dict[str, Any]:
    with _mailbox(args, select=_target_folder(args)) as (config, session):
        folders = session.list_folders()
        return {
            "ok": True,
            "account": redact(config),
            "folder": session.current_folder,
            "unread": session.count_unseen(),
            "folders": [f["name"] for f in folders],
            "gmail_raw": session.supports_gmail_raw(),
            "quota": session.quota_summary(),
        }


def cmd_folders(args: Dict[str, Any]) -> Dict[str, Any]:
    with _mailbox(args) as (_config, session):
        return {"ok": True, "folders": session.list_folders()}


def cmd_search(args: Dict[str, Any]) -> Dict[str, Any]:
    limit = max(1, int(args.get("limit") or 20))
    offset = max(0, int(args.get("offset") or 0))
    with _mailbox(args, select=_target_folder(args)) as (_config, session):
        folder = session.current_folder
        uids = session.search(
            str(args.get("query") or "ALL"),
            since=str(args.get("since") or ""),
            before=str(args.get("before") or ""),
            unread_only=bool(args.get("unread_only")),
            flagged_only=bool(args.get("flagged_only")),
            has_attachment=bool(args.get("has_attachment")),
            gmail_raw=str(args.get("gmail_raw") or ""),
        )
        total = len(uids)
        # Newest first for display; the caller still addresses UIDs.
        window = list(reversed(uids))[offset:offset + limit]
        messages = session.fetch_headers(window)
        messages.reverse()
        unread = session.count_unseen()
    return {
        "ok": True,
        "folder": folder,
        "matched": total,
        "returned": len(messages),
        "offset": offset,
        "has_more": offset + len(messages) < total,
        "unread_in_folder": unread,
        "messages": messages,
    }


def cmd_read(args: Dict[str, Any]) -> Dict[str, Any]:
    mark_read = bool(args.get("mark_read"))
    with _mailbox(args, select=_target_folder(args)) as (_config, session):
        folder = session.current_folder
        uids = _resolve_uids(args, session)
        messages = session.fetch_messages(
            uids,
            include_html=bool(args.get("include_html", False)),
            max_body_chars=int(args.get("max_body_chars") or 0),
        )
        if mark_read and messages:
            session.set_flags(uids, ["\\Seen"], mode="add")
    for item in messages:
        # A short preview keeps list output useful when many mails are read.
        item["preview"] = summarize_text(item.get("text", ""), 160)
    return {
        "ok": True,
        "folder": folder,
        "count": len(messages),
        "marked_read": mark_read,
        "messages": messages,
    }


def _build_from_args(args: Dict[str, Any], config: Dict[str, Any]) -> tuple:
    """Assemble a message from a command payload.

    Shared by ``send`` and ``draft`` so both accept the same headers; a draft
    just skips delivery.
    """
    to = _as_list(args.get("to"))
    return build_message(
        sender=str(config.get("send_as") or config["address"]),
        sender_name=str(args.get("sender_name") or config.get("sender_name") or ""),
        to=to or ["undisclosed-recipients:;"],
        subject=str(args.get("subject") or ""),
        text=str(args.get("text") or ""),
        html=str(args.get("html") or ""),
        cc=_as_list(args.get("cc")),
        bcc=_as_list(args.get("bcc")),
        reply_to=str(args.get("reply_to") or ""),
        in_reply_to=str(args.get("in_reply_to") or ""),
        references=_as_list(args.get("references")),
        attachments=_as_list(args.get("attachments")),
        attachment_paths=Path(args["attachment_base"]).expanduser() if args.get("attachment_base") else None,
        extra_headers=args.get("headers") if isinstance(args.get("headers"), dict) else None,
    )


def cmd_send(args: Dict[str, Any]) -> Dict[str, Any]:
    config = load_config(args.get("config_path", ""), args)
    if not _as_list(args.get("to")):
        raise MailError("'to' is required (string, list, or ',' separated)")
    message, summary = _build_from_args(args, config)
    if args.get("dry_run"):
        return {"ok": True, "dry_run": True, "would_send": summary,
                "size": len(message.as_bytes())}
    result = smtp_send(config, message)
    result["summary"] = summary
    return result


def cmd_draft(args: Dict[str, Any]) -> Dict[str, Any]:
    config = load_config(args.get("config_path", ""), args)
    message, summary = _build_from_args(args, config)
    # Marks the message as an unsent draft for clients that look for it.
    message["X-Unsent"] = "1"
    with ImapSession(config) as session:
        folder = session.append(str(args.get("folder") or "Drafts"), message)
    return {"ok": True, "draft_saved_in": folder, "summary": summary}


def cmd_flag(args: Dict[str, Any]) -> Dict[str, Any]:
    mode = str(args.get("mode") or "add").lower()
    if mode not in {"add", "remove"}:
        raise MailError("mode must be add or remove")
    requested = _as_list(args.get("flags"))
    add, remove = _plan_flag_changes(requested, mode)
    if not add and not remove:
        raise MailError("no usable flag names given")
    with _mailbox(args, select=_target_folder(args)) as (_config, session):
        folder = session.current_folder
        uids = _resolve_uids(args, session)
        counts: Dict[str, int] = {}
        if add:
            counts["added_count"] = session.set_flags(uids, add, mode="add")["updated"]
        if remove:
            counts["cleared_count"] = session.set_flags(uids, remove, mode="remove")["updated"]
    # `added`/`cleared` are the requested IMAP flags; the counts are separate
    # keys so one name never carries two different types.
    return {
        "ok": True, "folder": folder, "uids": uids, "requested": requested,
        "mode": mode, "added": add, "cleared": remove, "updated": len(uids),
        **counts,
    }


def cmd_move(args: Dict[str, Any]) -> Dict[str, Any]:
    destination = str(args.get("to_folder") or args.get("destination") or "")
    if not destination:
        raise MailError("'to_folder' is required")
    with _mailbox(args, select=_target_folder(args)) as (_config, session):
        folder = session.current_folder
        uids = _resolve_uids(args, session)
        target = session.move(uids, destination)
    return {"ok": True, "from": folder, "to": target, "moved": len(uids), "uids": uids}


def cmd_delete(args: Dict[str, Any]) -> Dict[str, Any]:
    permanent = bool(args.get("permanent"))
    with _mailbox(args, select=_target_folder(args)) as (_config, session):
        folder = session.current_folder
        uids = _resolve_uids(args, session)
        target = ""
        if not permanent:
            try:
                target = session.move(uids, "Trash")
            except MailError:
                target = ""  # No Trash folder on this account; fall through.
        if target:
            result = {"deleted": len(uids), "expunged": False, "moved_to": target}
        else:
            session.set_flags(uids, ["\\Deleted"], mode="add")
            session.expunge()
            result = {"deleted": len(uids), "expunged": True}
            if not permanent:
                result["note"] = "no Trash folder on this account; deleted permanently"
    result.update({"ok": True, "from": folder, "uids": uids})
    return result


def cmd_download(args: Dict[str, Any]) -> Dict[str, Any]:
    uid = str(args.get("uid") or "").strip()
    if not uid:
        raise MailError("'uid' is required")
    target_dir = Path(str(args.get("dir") or ".")).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    with _mailbox(args, select=_target_folder(args)) as (_config, session):
        folder = session.current_folder
        raw = session.fetch_raw(uid)
    msg = email.message_from_bytes(raw)
    wanted = {n.strip() for n in _as_list(args.get("names"))}
    saved: List[Dict[str, Any]] = []
    for part in msg.walk():
        filename = decode_header_value(part.get_filename() or "")
        if not filename or (wanted and filename not in wanted):
            continue
        payload = part.get_payload(decode=True) or b""
        path = unique_path(target_dir, safe_filename(filename))
        path.write_bytes(payload)
        saved.append({"filename": path.name, "source_name": filename,
                      "path": str(path), "size": len(payload),
                      "content_type": part.get_content_type()})
    if not saved:
        raise MailError("no matching attachments on that message")
    return {"ok": True, "folder": folder, "uid": uid, "saved": saved}


COMMANDS = {
    "check": cmd_check,
    "folders": cmd_folders,
    "search": cmd_search,
    "read": cmd_read,
    "send": cmd_send,
    "draft": cmd_draft,
    "flag": cmd_flag,
    "move": cmd_move,
    "delete": cmd_delete,
    "download": cmd_download,
}
