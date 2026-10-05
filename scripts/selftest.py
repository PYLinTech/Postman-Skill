#!/usr/bin/env python3
"""Self-check for the email skill.

Ships with the skill so the offline behaviour is reproducible by whoever
installs it, without a separate test tree:

    python mail.py selftest          # offline: parsing, MIME, servers, flags
    python mail.py selftest --live   # additionally connects to the mailbox

The offline half needs no network and no credentials. The live half reads the
same credentials as every other command and only performs read-only IMAP work,
so it is safe to run against a real account.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mailkit.commands import _plan_flag_changes, _target_folder  # noqa: E402
from mailkit.config import (  # noqa: E402
    build_imap_id,
    detect_provider,
    resolve_servers,
    smtp_security_for_port,
)
from mailkit.imap_client import _special_key, normalise_folder  # noqa: E402
from mailkit.mime import (  # noqa: E402
    build_message,
    html_to_text,
    parse_message,
    safe_filename,
    summarize_text,
    unique_path,
)


def _enc(text: str, charset: str = "utf-8") -> str:
    return f"=?{charset}?B?{base64.b64encode(text.encode(charset)).decode()}?="


def _checks() -> List[Tuple[str, Callable[[], Any], Any]]:
    """(name, thunk, expected) triples. Each thunk must be side-effect free."""
    cases: List[Tuple[str, Callable[[], Any], Any]] = []

    def add(name: str, thunk: Callable[[], Any], expected: Any) -> None:
        cases.append((name, thunk, expected))

    # -- header decoding ------------------------------------------------
    add("header/utf8", lambda: _dec(_enc("订单 通知")), "订单 通知")
    add("header/adjacent", lambda: _dec(_enc("订单") + " " + _enc("通知")), "订单通知")
    add("header/mislabelled", lambda: _dec(_enc("中文", "gb2312")), "中文")
    add("header/plain", lambda: _dec("plain subject"), "plain subject")
    add("header/empty", lambda: _dec(None), "")

    # -- HTML to text ---------------------------------------------------
    add("html/drops-script", lambda: "alert" in html_to_text("<script>alert(1)</script><p>ok</p>"), False)
    add("html/drops-style", lambda: "color" in html_to_text("<style>p{color:red}</style><p>ok</p>"), False)
    add("html/entities", lambda: "A&B" in html_to_text("<p>A&amp;B</p>"), True)
    add("html/table-tabs", lambda: "\t" in html_to_text("<table><tr><td>a</td><td>b</td></tr></table>"), True)
    add("html/list-marker", lambda: "- a" in html_to_text("<ul><li>a</li></ul>"), True)
    add("html/link-href", lambda: "https://x.test/1" in html_to_text('<a href="https://x.test/1">go</a>'), True)
    add("html/link-no-dup", lambda: html_to_text('<a href="https://x.test/1">https://x.test/1</a>').count("https://x.test/1"), 1)
    add("html/chinese", lambda: html_to_text("<p>您的订单已发货</p>"), "您的订单已发货")
    add("html/empty", lambda: html_to_text("   "), "")
    add("html/preview-one-line", lambda: "\n" in summarize_text("<p>a</p><p>b</p>"), False)

    # -- message parsing ------------------------------------------------
    def html_only() -> Dict[str, Any]:
        raw = ("From: a@qq.com\r\nSubject: s\r\nMIME-Version: 1.0\r\n"
               "Content-Type: text/html; charset=utf-8\r\n\r\n"
               "<p>请查收附件报价单</p>").encode()
        return parse_message(raw)

    add("parse/html-fallback", lambda: html_only()["text_source"], "html")
    add("parse/html-text", lambda: html_only()["text"], "请查收附件报价单")
    add("parse/subject-decoded", lambda: html_only()["subject"], "s")
    add("parse/no-html-flag", lambda: "html" in parse_message(
        "From: a@b\r\nMIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>x</p>".encode(),
        include_html=False), False)

    def multipart() -> Dict[str, Any]:
        raw = ("From: a@qq.com\r\nTo: r@163.com\r\nMIME-Version: 1.0\r\n"
               "Content-Type: multipart/mixed; boundary=\"B\"\r\n\r\n"
               "--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n见附件\r\n"
               "--B\r\nContent-Type: application/pdf\r\n"
               "Content-Disposition: attachment; filename=\"report.pdf\"\r\n"
               "Content-Transfer-Encoding: base64\r\n\r\naGVsbG8=\r\n--B--\r\n").encode()
        return parse_message(raw)

    add("parse/attachment-name", lambda: multipart()["attachments"][0]["filename"], "report.pdf")
    add("parse/attachment-size", lambda: multipart()["attachments"][0]["size"], 5)
    add("parse/plain-preferred", lambda: multipart()["text_source"], "plain")

    # -- message building -----------------------------------------------
    def built() -> Tuple[Any, Dict[str, Any]]:
        return build_message(sender="me@qq.com", to=["a@b.com"], subject="会议纪要",
                             text="正文", html="<p>正文</p>", bcc=["secret@e.com"])

    add("build/subject-roundtrip", lambda: parse_message(built()[0].as_bytes())["subject"], "会议纪要")
    add("build/alternative", lambda: built()[1]["format"], "multipart/alternative")
    add("build/html-only", lambda: build_message(sender="a@b.com", to=["c@d.com"],
                                                 subject="s", html="<p>x</p>")[1]["format"], "text/html")
    add("build/bcc-hidden", lambda: "secret@e.com" in (
        built()[0]["To"] + (built()[0].get("Cc") or "")), False)
    add("build/needs-recipient", lambda: _raises(lambda: build_message(
        sender="a@b.com", to=[], subject="s", text="t")), True)
    add("build/needs-body", lambda: _raises(lambda: build_message(
        sender="a@b.com", to=["c@d.com"], subject="s")), True)
    add("build/missing-attachment", lambda: _raises(lambda: build_message(
        sender="a@b.com", to=["c@d.com"], subject="s", text="t",
        attachments=["/nonexistent/x.txt"])), True)

    # -- flag planning --------------------------------------------------
    add("flag/unread-clears", lambda: _plan_flag_changes(["unread"], "add"), ([], ["\\Seen"]))
    add("flag/read-sets", lambda: _plan_flag_changes(["read"], "add"), (["\\Seen"], []))
    add("flag/star-add", lambda: _plan_flag_changes(["star"], "add"), (["\\Flagged"], []))
    add("flag/star-remove", lambda: _plan_flag_changes(["star"], "remove"), ([], ["\\Flagged"]))
    add("flag/remove-inverts-state", lambda: _plan_flag_changes(["read"], "remove"), ([], ["\\Seen"]))
    add("flag/last-wins", lambda: _plan_flag_changes(["read", "unread"], "add"), ([], ["\\Seen"]))
    add("flag/dedupes", lambda: _plan_flag_changes(["star", "starred", "flagged"], "add"), (["\\Flagged"], []))

    # -- servers --------------------------------------------------------
    add("server/163", lambda: resolve_servers("a@163.com")[0][0], "imap.163.com")
    add("server/126-distinct", lambda: resolve_servers("a@126.com")[0][0] != resolve_servers("a@163.com")[0][0], True)
    add("server/yeah", lambda: resolve_servers("a@yeah.net")[0][0], "imap.yeah.net")
    add("server/qq", lambda: resolve_servers("a@foxmail.com")[1], "foxmail")
    add("server/gmail", lambda: resolve_servers("a@gmail.com")[0][0], "imap.gmail.com")
    add("server/provider-override", lambda: resolve_servers("a@163.com", "qq")[1], "qq")
    add("server/host-override", lambda: resolve_servers("a@163.com", overrides={"imap_host": "imap.corp.test"})[0][0], "imap.corp.test")
    add("server/detect", lambda: detect_provider("a@outlook.com"), "outlook")
    add("server/unknown-provider", lambda: _raises(lambda: resolve_servers("a@b.com", "nope")), True)
    add("server/ssl-465", lambda: smtp_security_for_port(465), "ssl")
    add("server/starttls-587", lambda: smtp_security_for_port(587), "starttls")
    add("server/plain-25", lambda: smtp_security_for_port(25), "none")

    # -- folders --------------------------------------------------------
    add("folder/drafts-plural", lambda: _special_key("Drafts"), _special_key("\\Draft"))
    add("folder/sent", lambda: _special_key("Sent"), _special_key("\\Sent"))
    add("folder/alias-inbox", lambda: normalise_folder("INBOX"), "INBOX")
    add("folder/alias-sent", lambda: normalise_folder("sent"), "Sent")
    add("folder/alias-trash", lambda: normalise_folder("deleted"), "Trash")
    add("folder/target-default", lambda: _target_folder({}), "INBOX")

    # -- IMAP ID --------------------------------------------------------
    add("id/default", lambda: build_imap_id().count('"python-mailkit"'), 2)
    add("id/custom", lambda: build_imap_id("acme").count('"acme"'), 2)

    # -- body size guard ------------------------------------------------
    def long_message(limit: int, include_html: bool = False) -> Dict[str, Any]:
        body = "<div>" + ("内容" * 8000) + "</div>"
        raw = ("From: a@b.com\r\nSubject: 长\r\nMIME-Version: 1.0\r\n"
               "Content-Type: text/html; charset=utf-8\r\n\r\n" + body).encode()
        return parse_message(raw, include_html=include_html, max_body_chars=limit)

    add("body/defaults-to-no-html", lambda: "html" in long_message(0), False)
    add("body/html-opt-in", lambda: "html" in long_message(0, True), True)
    add("body/truncates", lambda: long_message(500)["truncated"], True)
    add("body/keeps-length", lambda: len(long_message(500)["text"]), 500)
    add("body/reports-original", lambda: long_message(500)["text_chars"] > 500, True)
    add("body/zero-disables", lambda: long_message(0)["truncated"], False)
    add("body/under-limit-not-truncated", lambda: long_message(999999)["truncated"], False)

    # -- multi-account selection ----------------------------------------
    _ACCOUNTS = {"accounts": {"work": {"address": "w@sohu.com", "password": "p"},
                              "home": {"address": "h@sina.com", "password": "p"}}}

    def selected(data: Any, name: str) -> Any:
        """Load *name* from a config file, keeping the temp dir alive."""
        from mailkit.config import ConfigError, load_config

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mail.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            try:
                return load_config(path, {"account": name})
            except ConfigError as exc:
                return f"ERR:{exc}"

    add("account/picks-work", lambda: selected(_ACCOUNTS, "work")["imap_host"], "imap.sohu.com")
    add("account/picks-home", lambda: selected(_ACCOUNTS, "home")["imap_host"], "imap.sina.com")
    add("account/unknown-fails", lambda: str(selected(_ACCOUNTS, "ghost")).startswith("ERR:"), True)
    add("account/error-lists-available",
        lambda: "available: home, work" in str(selected(_ACCOUNTS, "ghost")), True)
    add("account/missing-map-fails",
        lambda: str(selected({"address": "a@qq.com", "password": "p"}, "work")).startswith("ERR:"),
        True)

    # -- filenames ------------------------------------------------------
    add("file/strips-traversal", lambda: "/" in safe_filename("../../etc/passwd"), False)
    add("file/strips-backslash", lambda: "\\" in safe_filename(r"C:\x.exe"), False)
    add("file/keeps-unicode", lambda: safe_filename("报表.pdf"), "报表.pdf")
    add("file/blank-fallback", lambda: safe_filename("   "), "attachment")
    add("file/collision", lambda: _collide("a.pdf"), "a(1).pdf")
    add("file/collision-no-ext", lambda: _collide("README"), "README(1)")

    return cases


def _dec(value: Any) -> str:
    from mailkit.mime import decode_header_value

    return decode_header_value(value)


def _raises(thunk: Callable[[], Any]) -> bool:
    try:
        thunk()
    except Exception:
        return True
    return False


def _collide(name: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / name).write_text("x")
        return unique_path(root, name).name


def run_offline() -> Tuple[int, int, List[str]]:
    failures: List[str] = []
    cases = _checks()
    for name, thunk, expected in cases:
        try:
            got = thunk()
        except Exception as exc:  # noqa: BLE001 - report as a check failure
            failures.append(f"{name}: raised {type(exc).__name__}: {exc}")
            continue
        if got != expected:
            failures.append(f"{name}: got {got!r}, want {expected!r}")
    return len(cases), len(failures), failures


def run_live() -> Tuple[int, int, List[str]]:
    """Read-only connectivity probes against the configured mailbox."""
    results: List[Tuple[str, Callable[[], Any], Any]] = [
        ("imap-connect", _live_connect, True),
        ("select-inbox", _live_select, "INBOX"),
        ("list-folders", lambda: bool(_folders()), True),
        ("search", lambda: isinstance(_search(), list), True),
    ]
    failures: List[str] = []
    try:
        for name, thunk, expected in results:
            try:
                got = thunk()
            except Exception as exc:  # noqa: BLE001 - report as a check failure
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            if got != expected:
                failures.append(f"{name}: got {got!r}, want {expected!r}")
    finally:
        if _SESSION.get("session") is not None:
            _SESSION["session"].close()
    return len(results), len(failures), failures


_SESSION: Dict[str, Any] = {}


def _live_connect() -> bool:
    from mailkit.config import load_config
    from mailkit.imap_client import ImapSession

    config = load_config("", {})
    _SESSION["config"] = {
        "address": config["address"],
        "imap_host": config["imap_host"],
        "imap_port": config["imap_port"],
    }
    _SESSION["session"] = ImapSession(config)
    _SESSION["session"].connect()
    return True


def _live_select() -> Any:
    return _SESSION["session"].select("INBOX")


def _folders() -> Any:
    return _SESSION["session"].list_folders()


def _search() -> Any:
    return _SESSION["session"].search("ALL", limit=1)


def run(live: bool = False) -> Dict[str, Any]:
    """Run the checks and return a JSON-serialisable report.

    This is the single entry point; :func:`main` only adds printing.
    """
    payload: Dict[str, Any] = {"ok": True, "mode": "offline+live" if live else "offline"}

    total, failed, failures = run_offline()
    payload["offline"] = {"total": total, "failed": failed, "failures": failures}

    if live:
        ltotal, lfailed, lfailures = run_live()
        payload["live"] = {"total": ltotal, "failed": lfailed, "failures": lfailures}

    passed = total - failed
    summary = f"{passed}/{total} offline checks passed"
    if live:
        info = payload["live"]
        summary += f", {info['total'] - info['failed']}/{info['total']} live"
    payload["summary"] = summary
    payload["ok"] = failed + payload.get("live", {}).get("failed", 0) == 0
    return payload


def main(argv: List[str]) -> int:
    report = run(live="--live" in argv)
    for section in ("offline", "live"):
        for name in report.get(section, {}).get("failures", []):
            print(f"  FAIL {section}: {name}", file=sys.stderr)

    if "--json" in argv:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(report["summary"])
        if "--live" in argv and report.get("live", {}).get("failures"):
            print("\nlive failures (need a working account):", file=sys.stderr)
            for name in report["live"]["failures"]:
                print(f"  {name}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
