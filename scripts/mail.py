#!/usr/bin/env python3
"""email — a general-purpose IMAP/SMTP command line client.

Standard library only. No third-party dependency.

    mail.py check
    mail.py search --unread --limit 10
    mail.py read --uid 12345
    mail.py send --to a@b.com --subject "Hi" --html '<p>Hello</p>'
    mail.py json '{"command": "search", "query": "UNSEEN"}'

``json`` is a generic escape hatch for callers that prefer to pass a single
object (shell quoting is awkward otherwise); it is not required.

Output is human-readable by default; pass ``--json`` for structured output.
Run ``mail.py <command> --help`` for per-command options.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mailkit import commands  # noqa: E402
from mailkit.config import ConfigError  # noqa: E402
from mailkit.mime import MailError  # noqa: E402

# Cap on the characters a single message body may contribute. Newsletters and
# long threads otherwise flood whatever agent is reading the result.
DEFAULT_MAX_BODY_CHARS = 20000

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_MAIL = 4
EXIT_INTERNAL = 5


# --------------------------------------------------------------------------
# Argument plumbing
# --------------------------------------------------------------------------

def _split_list(values: Optional[List[str]]) -> List[str]:
    """Flatten repeatable flags and comma/semicolon separated strings."""
    out: List[str] = []
    for value in values or []:
        for part in str(value).replace(";", ",").split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _read_text(inline: Optional[str], path: Optional[str], what: str) -> str:
    if inline and path:
        raise MailError(f"give either --{what} or --{what}-file, not both")
    if path:
        try:
            with open(os.path.expanduser(path), "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            raise MailError(f"cannot read --{what}-file {path}: {exc}") from exc
    return inline or ""


def _base_args(namespace: argparse.Namespace) -> Dict[str, Any]:
    return {
        "config_path": namespace.config,
        "account": namespace.account,
    }


# --------------------------------------------------------------------------
# Per-command builders
# --------------------------------------------------------------------------

def _cmd_check(ns: argparse.Namespace) -> Dict[str, Any]:
    args = _base_args(ns)
    if ns.folder:
        args["folder"] = ns.folder
    return commands.cmd_check(args)


def _cmd_folders(ns: argparse.Namespace) -> Dict[str, Any]:
    return commands.cmd_folders(_base_args(ns))


def _cmd_search(ns: argparse.Namespace) -> Dict[str, Any]:
    args = _base_args(ns)
    args.update({
        "folder": ns.folder,
        "query": ns.query,
        "since": ns.since,
        "before": ns.before,
        "unread_only": ns.unread,
        "flagged_only": ns.flagged,
        "has_attachment": ns.has_attachment,
        "gmail_raw": ns.gmail_raw,
        "limit": ns.limit,
        "offset": ns.offset,
    })
    return commands.cmd_search(args)


def _cmd_read(ns: argparse.Namespace) -> Dict[str, Any]:
    args = _base_args(ns)
    args.update({
        "uid": _split_list([ns.uid]),
        "folder": ns.folder,
        "include_html": ns.html,
        "mark_read": ns.mark_read,
        "max_body_chars": ns.max_chars,
    })
    if ns.latest:
        args.pop("uid")
        args["query"] = "ALL"
        args["limit"] = ns.latest
    return commands.cmd_read(args)


def _message_args(ns: argparse.Namespace) -> Dict[str, Any]:
    args = _base_args(ns)
    args.update({
        "to": _split_list(ns.to),
        "cc": _split_list(ns.cc),
        "bcc": _split_list(ns.bcc),
        "subject": ns.subject or "",
        "text": _read_text(ns.text, ns.text_file, "text"),
        "html": _read_text(ns.html, ns.html_file, "html"),
        "attachments": ns.attach or [],
        "attachment_base": os.getcwd(),
        "reply_to": ns.reply_to or "",
        "in_reply_to": ns.in_reply_to or "",
        "references": _split_list(ns.reference),
    })
    if not args["text"] and not args["html"]:
        raise MailError("a body is required: --text/--text-file or --html/--html-file")
    return args


def _cmd_send(ns: argparse.Namespace) -> Dict[str, Any]:
    args = _message_args(ns)
    if ns.dry_run:
        args["dry_run"] = True
    return commands.cmd_send(args)


def _cmd_draft(ns: argparse.Namespace) -> Dict[str, Any]:
    args = _message_args(ns)
    args["folder"] = ns.folder
    return commands.cmd_draft(args)


def _cmd_flag(ns: argparse.Namespace) -> Dict[str, Any]:
    args = _base_args(ns)
    args.update({
        "uid": _split_list([ns.uid]),
        "folder": ns.folder,
        "flags": _split_list([ns.flags]),
        "mode": ns.mode,
    })
    return commands.cmd_flag(args)


def _cmd_move(ns: argparse.Namespace) -> Dict[str, Any]:
    args = _base_args(ns)
    args.update({
        "uid": _split_list([ns.uid]),
        "folder": ns.folder,
        "to_folder": ns.to_folder,
    })
    return commands.cmd_move(args)


def _cmd_delete(ns: argparse.Namespace) -> Dict[str, Any]:
    args = _base_args(ns)
    args.update({
        "uid": _split_list([ns.uid]),
        "folder": ns.folder,
        "permanent": ns.permanent,
    })
    return commands.cmd_delete(args)


def _cmd_download(ns: argparse.Namespace) -> Dict[str, Any]:
    args = _base_args(ns)
    args.update({
        "uid": ns.uid,
        "folder": ns.folder,
        "dir": ns.dir,
        "names": _split_list([ns.name]),
    })
    return commands.cmd_download(args)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _render_check(data: Dict[str, Any]) -> str:
    account = data.get("account", {})
    return "\n".join([
        f"account    {account.get('address', '?')}",
        f"provider   {account.get('provider') or '(auto)'}",
        f"imap       {account.get('imap_host')}:{account.get('imap_port')}",
        f"smtp       {account.get('smtp_host')}:{account.get('smtp_port')}",
        f"folder     {data.get('folder')}   unread: {data.get('unread')}",
        f"folders    {len(data.get('folders', []))}",
    ])


def _render_folders(data: Dict[str, Any]) -> str:
    return "\n".join(
        f"  {f['name']}" + (f"  [{f['special']}]" if f.get("special") else "")
        for f in data.get("folders", [])
    ) or "(no folders)"


def _render_search(data: Dict[str, Any]) -> str:
    lines = [f"{data['folder']}  matched={data['matched']}  shown={data['returned']}"
             f"  unread={data['unread_in_folder']}"]
    for m in data.get("messages", []):
        lines.append(f"  uid={m.get('uid', '?'):>8}  {m.get('date', ''):<32}  {m.get('subject', '')}")
        lines.append(f"             from: {m.get('from', '')}")
    if data.get("has_more"):
        remaining = data["matched"] - data["offset"] - data["returned"]
        lines.append(f"  ... {remaining} more; raise --limit or set --offset")
    return "\n".join(lines)


def _render_read(data: Dict[str, Any]) -> str:
    blocks = []
    for m in data.get("messages", []):
        head = [
            f"uid        {m.get('uid')}",
            f"from       {m.get('from')}",
            f"to         {', '.join(m.get('to', []))}",
        ]
        if m.get("cc"):
            head.append(f"cc         {', '.join(m['cc'])}")
        head += [
            f"date       {m.get('date')}",
            f"subject    {m.get('subject')}",
            f"body       {m.get('text_source')} ({m.get('size', 0)} bytes)",
        ]
        if m.get("truncated"):
            head.append(f"truncated  showing {len(m.get('text', ''))} of "
                        f"{m.get('text_chars', '?')} characters "
                        "(raise --max-chars or 0 for all)")
        if m.get("attachments"):
            head.append("attachments " + ", ".join(
                f"{a['filename']} ({a['size']}B, {a['content_type']})" for a in m["attachments"]))
        blocks.append("\n".join(head) + "\n\n" + m.get("text", ""))
    return ("\n\n" + "-" * 60 + "\n\n").join(blocks) or "(no messages)"


def _render_send(data: Dict[str, Any]) -> str:
    summary = data.get("summary", {})
    return (f"sent from {data.get('from', '?')}\n"
            f"  via {data.get('server', '?')} ({data.get('transport', '?')})\n"
            f"  to   {', '.join(summary.get('to', []))}\n"
            f"  subj {summary.get('subject', '')}")


def _render_draft(data: Dict[str, Any]) -> str:
    summary = data.get("summary", {})
    return (f"draft saved in {data.get('draft_saved_in', '?')}\n"
            f"  to   {', '.join(summary.get('to', []))}\n"
            f"  subj {summary.get('subject', '')}")


def _render_dry_run(data: Dict[str, Any]) -> str:
    return "DRY RUN — nothing sent\n" + json.dumps(
        data.get("would_send", {}), ensure_ascii=False, indent=2)


def _render_flag(data: Dict[str, Any]) -> str:
    parts = []
    if data.get("added_count"):
        parts.append(f"added {data['added_count']} ({', '.join(data.get('added') or [])})")
    if data.get("cleared_count"):
        parts.append(f"cleared {data['cleared_count']} ({', '.join(data.get('cleared') or [])})")
    done = "; ".join(parts) or "nothing to change"
    return f"{done} on {len(data.get('uids', []))} message(s) in {data.get('folder')}"


def _render_delete(data: Dict[str, Any]) -> str:
    where = f"moved to {data['moved_to']}" if data.get("moved_to") else "expunged"
    note = f"  ({data['note']})" if data.get("note") else ""
    return f"deleted {data.get('deleted', 0)} message(s) — {where}{note}"


def _render_download(data: Dict[str, Any]) -> str:
    return "\n".join(f"  saved {s['path']}  ({s['size']}B)" for s in data.get("saved", []))


def _render_selftest(data: Dict[str, Any]) -> str:
    lines = [data.get("summary", "")]
    for section in ("offline", "live"):
        info = data.get(section) or {}
        for name in info.get("failures", []):
            lines.append(f"  FAIL {section}: {name}")
    return "\n".join(lines)


_RENDERERS = {
    "check": _render_check,
    "folders": _render_folders,
    "search": _render_search,
    "read": _render_read,
    "send": _render_send,
    "draft": _render_draft,
    "flag": _render_flag,
    "delete": _render_delete,
    "download": _render_download,
    "selftest": _render_selftest,
}


def _render(command: str, data: Dict[str, Any]) -> str:
    """Human-readable rendering. The JSON output stays the source of truth."""
    if data.get("dry_run"):
        return _render_dry_run(data)
    renderer = _RENDERERS.get(command)
    if renderer is None:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return renderer(data)


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail.py",
        description="General-purpose IMAP/SMTP client (stdlib only).",
    )
    parser.add_argument("--config", metavar="PATH", help="credential JSON file")
    parser.add_argument("--account", metavar="NAME", help="named account inside that file")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, handler: Any, help_text: str) -> argparse.ArgumentParser:
        child = sub.add_parser(name, help=help_text, description=help_text)
        child.set_defaults(handler=handler)
        return child

    p = add("check", _cmd_check, "verify credentials and show the mailbox")
    p.add_argument("--folder", help="folder to select (default INBOX)")

    add("folders", _cmd_folders, "list mail folders")

    p = add("search", _cmd_search, "search messages; never marks mail as read")
    p.add_argument("--folder", "-f", default="INBOX", help="folder (default INBOX)")
    p.add_argument("--query", "-q", default="ALL", help="IMAP search criteria (default ALL)")
    p.add_argument("--gmail-raw", help="Gmail X-GM-RAW query, used verbatim when supported")
    p.add_argument("--since", help="SINCE date, YYYY-MM-DD")
    p.add_argument("--before", help="BEFORE date, YYYY-MM-DD")
    p.add_argument("--unread", action="store_true", help="unread only")
    p.add_argument("--flagged", action="store_true", help="starred only")
    p.add_argument("--has-attachment", action="store_true", help="multipart mail only")
    p.add_argument("--limit", "-n", type=int, default=20, help="max results (default 20)")
    p.add_argument("--offset", type=int, default=0, help="skip N newest first")

    p = add("read", _cmd_read, "read full message bodies")
    p.add_argument("--uid", help="message UID; omit with --latest")
    p.add_argument("--latest", type=int, metavar="N", help="read the N newest instead of --uid")
    p.add_argument("--folder", "-f", default="INBOX")
    p.add_argument("--mark-read", action="store_true", help="set the Seen flag (off by default)")
    p.add_argument("--html", action="store_true",
                   help="also return the raw HTML part (off by default; it is "
                        "markup, CSS and inline images the text already covers)")
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_BODY_CHARS,
                   metavar="N", help="truncate the body at N characters "
                                      f"(default {DEFAULT_MAX_BODY_CHARS}; 0 disables)")

    def add_message(name: str, handler: Any, help_text: str) -> None:
        p = add(name, handler, help_text)
        p.add_argument("--to", action="append", help="recipient (repeatable, or comma separated)")
        p.add_argument("--cc", action="append", help="Cc recipient")
        p.add_argument("--bcc", action="append", help="Bcc recipient (never shown in To/Cc)")
        p.add_argument("--subject", help="subject line")
        p.add_argument("--text", help="plain-text body")
        p.add_argument("--text-file", help="read the plain-text body from a file")
        p.add_argument("--html", help="HTML body")
        p.add_argument("--html-file", help="read the HTML body from a file")
        p.add_argument("--attach", action="append", help="attachment path (repeatable)")
        p.add_argument("--reply-to", help="Reply-To header")
        p.add_argument("--in-reply-to", help="Message-ID being replied to (threads the reply)")
        p.add_argument("--reference", action="append", help="extra References entry")

    add_message("send", _cmd_send, "send a message over SMTP")
    sub.choices["send"].add_argument("--dry-run", action="store_true",
                                     help="build and validate the message without sending")
    add_message("draft", _cmd_draft, "save a message to the Drafts folder")
    sub.choices["draft"].add_argument("--folder", "-f", default="Drafts",
                                      help="target draft folder (default Drafts)")

    p = add("flag", _cmd_flag, "change flags on messages")
    p.add_argument("--uid", required=True, help="message UID (repeatable)")
    p.add_argument("--folder", "-f", default="INBOX")
    p.add_argument("--flags", required=True,
                   help="read/unread, star, answered, deleted, draft (repeatable)")
    p.add_argument("--mode", choices=("add", "remove"), default="add",
                   help="add (default) or remove")

    p = add("move", _cmd_move, "move messages to another folder")
    p.add_argument("--uid", required=True, help="message UID (repeatable)")
    p.add_argument("--folder", "-f", default="INBOX", help="source folder")
    p.add_argument("--to-folder", required=True, help="destination folder")

    p = add("delete", _cmd_delete, "delete messages (Trash when present)")
    p.add_argument("--uid", required=True, help="message UID (repeatable)")
    p.add_argument("--folder", "-f", default="INBOX")
    p.add_argument("--permanent", action="store_true",
                   help="expunge instead of moving to Trash; cannot be undone")

    p = add("download", _cmd_download, "save attachments from one message")
    p.add_argument("--uid", required=True)
    p.add_argument("--folder", "-f", default="INBOX")
    p.add_argument("--dir", default=".", help="destination directory (default .)")
    p.add_argument("--name", action="append", help="only save this filename (repeatable)")

    p = add("selftest", _cmd_selftest, "verify this build; --live also connects to the mailbox")
    p.add_argument("--live", action="store_true",
                   help="also run read-only IMAP probes against the configured mailbox")

    p = add("json", None, "run a command from one JSON object")
    p.add_argument("payload", help='e.g. \'{"command":"search","query":"UNSEEN"}\'')
    p.set_defaults(handler=None)

    return parser


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _cmd_selftest(ns: argparse.Namespace) -> Dict[str, Any]:
    import selftest  # local module, avoids a package-level import cycle

    return selftest.run(live=ns.live)

def _run_json_command(ns: argparse.Namespace) -> Dict[str, Any]:
    """Dispatch the ``json`` escape hatch onto the normal command table."""
    try:
        payload = json.loads(ns.payload)
    except json.JSONDecodeError as exc:
        raise MailError(f"not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MailError("payload must be a JSON object")
    name = str(payload.pop("command", "")).strip().lower()
    handler = commands.COMMANDS.get(name)
    if handler is None:
        raise MailError(
            f"unknown command {name!r}; available: {', '.join(sorted(commands.COMMANDS))}"
        )
    if ns.config and "config_path" not in payload:
        payload["config_path"] = ns.config
    return handler(payload)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not getattr(ns, "command", None):
        parser.print_help()
        return EXIT_USAGE

    try:
        if ns.command == "json":
            result = _run_json_command(ns)
        else:
            result = ns.handler(ns)
    except ConfigError as exc:
        return _emit(ns, {"ok": False, "error": str(exc), "type": "config"},
                     "set MAIL_ADDRESS and MAIL_PASSWORD, or pass --config", EXIT_CONFIG)
    except MailError as exc:
        return _emit(ns, {"ok": False, "error": str(exc), "type": "mail"}, "", EXIT_MAIL)
    except KeyboardInterrupt:
        return _emit(ns, {"ok": False, "error": "interrupted", "type": "mail"}, "", 130)
    except Exception as exc:  # noqa: BLE001 - report unexpected failures as data
        return _emit(ns, {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                          "type": "internal"}, "", EXIT_INTERNAL)

    return _emit(ns, result, "", EXIT_OK)


def _emit(ns: argparse.Namespace, data: Dict[str, Any], hint: str, code: int) -> int:
    payload = dict(data)
    if hint and not payload.get("ok", False):
        payload["hint"] = hint
    if getattr(ns, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        command = getattr(ns, "command", "")
        if payload.get("ok") is False:
            print(f"error: {payload.get('error')}", file=sys.stderr)
            if hint:
                print(f"hint:  {hint}", file=sys.stderr)
        else:
            print(_render(command, payload))
    return code


if __name__ == "__main__":
    sys.exit(main())
