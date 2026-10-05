"""MIME parsing and building, including HTML mail support.

Everything here is pure-Python and offline so it can be unit-tested without a
mailbox. The HTML-to-text converter is the piece that matters most: a plain
``text/plain`` part is preferred when the sender supplied one, and HTML is only
converted as a fallback, so rich newsletters degrade to readable text instead of
returning nothing.
"""

from __future__ import annotations

import email
import email.header
import email.utils
import mimetypes
import re
from email.header import decode_header
from email.message import EmailMessage, Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Elements whose entire subtree never contributes readable text.
_SKIP_CONTENT = {"script", "style", "head", "noscript", "template", "svg", "canvas"}

# Elements that force a line break when they open or close.
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "center", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "tfoot", "thead", "tr", "ul",
}

_UNSAFE_FILENAME = re.compile(r'[\x00-\x1f/\\:*?"<>|]+')


class MailError(Exception):
    """Raised for user-facing failures (bad folder, missing UID, auth, ...)."""


# --------------------------------------------------------------------------
# Headers
# --------------------------------------------------------------------------

def decode_header_value(raw: Any) -> str:
    """Decode an RFC 2047 encoded header into text.

    Chinese subjects arrive as ``=?utf-8?B?...?=``; without this the caller sees
    raw base64. Falls back to the original string when a part cannot be decoded.
    """
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    if not text:
        return ""
    try:
        parts = decode_header(text)
    except Exception:
        return text
    out: List[str] = []
    for value, charset in parts:
        if isinstance(value, bytes):
            out.append(_decode_bytes(value, charset))
        else:
            out.append(value)
    return "".join(out).strip()


def _decode_bytes(value: bytes, charset: Optional[str]) -> str:
    """Decode one RFC 2047 chunk, trying the declared charset first.

    ``errors="replace"`` never raises, so the candidate list is walked with a
    strict decode and only the final attempt is allowed to substitute. Without
    that, a mislabelled charset silently yields mojibake.
    """
    candidates = [charset] if charset else []
    candidates += ["utf-8", "gb18030"]
    for encoding in candidates:
        if not encoding:
            continue
        try:
            return value.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="replace")


def format_address(value: str) -> str:
    """Render an address as ``Display Name <addr@host>`` for use in a header.

    ``email.utils.formataddr`` re-encodes a non-ASCII display name, which is
    correct for a header we are composing but wrong when we are reading one.
    Use :func:`display_address` for the parsing side.
    """
    name, addr = email.utils.parseaddr(str(value or ""))
    name = decode_header_value(name)
    if not addr:
        return name or str(value or "")
    return email.utils.formataddr((name, addr)) if name else addr


def display_address(value: Any) -> str:
    """Decode a header address for human/model consumption, without re-encoding."""
    name, addr = email.utils.parseaddr(decode_header_value(str(value or "")))
    name = name.strip()
    if name and addr:
        return f"{name} <{addr}>"
    return addr or name or str(value or "").strip()


def parse_address_list(value: Any) -> List[Tuple[str, str]]:
    """Return ``[(display_name, addr_spec), ...]`` for a header field."""
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split(";")
    out: List[Tuple[str, str]] = []
    for item in items:
        name, addr = email.utils.parseaddr(decode_header_value(item).strip())
        if addr:
            out.append((name, addr))
    return out


# --------------------------------------------------------------------------
# HTML -> text
# --------------------------------------------------------------------------

class _HTMLToText(HTMLParser):
    """Flatten HTML into readable plain text.

    Handles the shapes that actually show up in mail: block elements become
    line breaks, ``<br>`` is a hard break, list items get a bullet, table rows
    become one line per row, and links keep their href when the anchor text
    would otherwise lose the destination.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip_depth = 0
        self._href: Optional[str] = None
        self._anchor_text: List[str] = []

    # -- helpers ---------------------------------------------------------
    def _break(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def _newline(self) -> None:
        if self.parts and self.parts[-1] not in ("\n", "\n\n"):
            self.parts.append("\n\n")

    # -- parser hooks ----------------------------------------------------
    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_CONTENT:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self._newline()
            self.parts.append("- ")
        elif tag == "hr":
            self._newline()
            self.parts.append("---")
            self._newline()
        elif tag == "td" or tag == "th":
            self.parts.append("\t")
        elif tag in _BLOCK_TAGS:
            self._newline()
        elif tag == "a":
            href = dict(attrs).get("href")
            self._href = href.strip() if isinstance(href, str) else None
            self._anchor_text = []

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "br" and not self._skip_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_CONTENT:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "a" and self._href:
            # Only surface the URL when the visible text is not already the URL.
            label = "".join(self._anchor_text).strip()
            if self._href not in label and not self._href.lower().startswith("javascript:"):
                self.parts.append(f" <{self._href}>")
            self._href = None
            self._anchor_text = []
        elif tag == "li":
            self._break()
        elif tag in ("td", "th"):
            self.parts.append(" ")
        elif tag == "tr" or tag in _BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        if not data.strip():
            if self.parts and not self.parts[-1].endswith("\n"):
                # Preserve a single separating space between inline runs.
                self.parts.append(" ")
            return
        self.parts.append(data)
        if self._href is not None:
            self._anchor_text.append(data)

    def result(self) -> str:
        text = "".join(self.parts).replace("\r\n", "\n").replace("\r", "\n")
        # Collapse runs of spaces, and runs of tabs, but keep tabs as column
        # separators so table rows stay readable.
        lines = [re.sub(r" {2,}", " ", re.sub(r"\t{2,}", "\t", line)).strip(" \t") for line in text.split("\n")]
        out: List[str] = []
        blank = False
        for line in lines:
            if not line:
                if out and not blank:
                    out.append("")
                    blank = True
                continue
            blank = False
            out.append(line)
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    """Convert an HTML document or fragment to readable plain text."""
    if not html or not str(html).strip():
        return ""
    parser = _HTMLToText()
    try:
        parser.feed(str(html))
        parser.close()
    except Exception:
        # A malformed document should still yield whatever was parsed.
        pass
    text = parser.result()
    if text:
        return text
    # Last resort: strip tags outright so a broken document is not lost.
    stripped = re.sub(r"<[^>]+>", " ", str(html))
    stripped = re.sub(r"\s+", " ", stripped)
    return stripped.strip()


def summarize_text(html: str, limit: int = 120) -> str:
    """Derive a one-line summary from HTML, for list previews."""
    text = html_to_text(html)
    text = " ".join(text.split())
    return text[:limit]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    return _decode_bytes(payload, part.get_content_charset())


def safe_filename(name: str, fallback: str = "attachment") -> str:
    """Strip path separators and control characters from an attachment name."""
    cleaned = _UNSAFE_FILENAME.sub("_", str(name or "").strip())
    cleaned = cleaned.strip(". ") or fallback
    return cleaned[:120]


def unique_path(directory: Path, filename: str) -> Path:
    """Resolve *filename* inside *directory*, avoiding collisions on disk.

    Two parts in one message can share a name; silently overwriting the first
    would lose data, so ``report.pdf`` becomes ``report(1).pdf`` and onward.
    """
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, dot, suffix = filename.rpartition(".")
    if not dot:
        stem, suffix = filename, ""
    for index in range(1, 1000):
        attempt = f"{stem}({index}){'.' + suffix if suffix else ''}"
        candidate = directory / attempt
        if not candidate.exists():
            return candidate
    raise MailError(f"too many files named {filename!r} in {directory}")


def parse_message(
    raw: bytes,
    *,
    include_html: bool = False,
    max_body_chars: int = 0,
) -> Dict[str, Any]:
    """Parse a raw RFC 822 message into a JSON-serialisable dict.

    ``text`` is the readable body: the sender's ``text/plain`` part when
    present, otherwise the HTML part converted. ``text_source`` records which
    path was taken so the caller can tell an HTML-only mail from an empty one.

    ``include_html`` is off by default: the extracted text is what a reader
    wants, while the raw part mostly carries markup, CSS and inline base64
    images that would multiply the context an agent has to read.

    ``max_body_chars`` truncates the body when positive and records the
    original length, so a caller can tell a short mail from a cut one.
    """
    msg = email.message_from_bytes(raw)

    plain_parts: List[str] = []
    html_parts: List[str] = []
    attachments: List[Dict[str, Any]] = []
    related: List[Dict[str, Any]] = []
    in_related = False

    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            subtype = (part.get_content_subtype() or "").lower()
            if subtype == "related":
                in_related = True
            continue
        disposition = (part.get_content_disposition() or "").lower()
        ctype = part.get_content_type()
        filename = part.get_filename()
        if filename:
            filename = decode_header_value(filename)

        if disposition == "attachment" or (filename and ctype not in ("text/plain", "text/html")):
            payload = part.get_payload(decode=True) or b""
            attachments.append({
                "filename": safe_filename(filename or "", f"part-{len(attachments) + 1}"),
                "content_type": ctype,
                "size": len(payload),
            })
            continue

        if ctype == "text/plain":
            body = _part_text(part)
            if body and not in_related:
                plain_parts.append(body)
        elif ctype == "text/html":
            body = _part_text(part)
            if body:
                html_parts.append(body)
        elif ctype.startswith("image/") and in_related:
            related.append({
                "content_type": ctype,
                "content_id": (part.get("Content-ID") or "").strip("<>"),
                "size": len(part.get_payload(decode=True) or b""),
            })

    html = "\n".join(html_parts).strip()
    plain = "\n\n".join(p for p in plain_parts if p.strip()).strip()

    if plain:
        text, source = plain, "plain"
    elif html:
        text, source = html_to_text(html), "html"
    else:
        text, source = "", "empty"

    body_bytes = len(raw)
    original_chars = len(text)
    truncated = False
    if max_body_chars and original_chars > max_body_chars:
        text = text[:max_body_chars]
        truncated = True

    result: Dict[str, Any] = {
        "message_id": decode_header_value(msg.get("Message-ID", "")),
        "in_reply_to": decode_header_value(msg.get("In-Reply-To", "")),
        "references": decode_header_value(msg.get("References", "")),
        "subject": decode_header_value(msg.get("Subject", "")),
        "from": display_address(msg.get("From", "")),
        "to": [display_address(a) for _, a in parse_address_list(msg.get("To", ""))],
        "cc": [display_address(a) for _, a in parse_address_list(msg.get("Cc", ""))],
        "date": decode_header_value(msg.get("Date", "")),
        "text": text,
        "text_source": source,
        "text_chars": original_chars,
        "truncated": truncated,
        "attachments": attachments,
        "size": body_bytes,
    }
    if include_html and html:
        result["html"] = html
    if related:
        result["inline_images"] = related
    return result


def parse_headers_only(raw: bytes) -> Dict[str, str]:
    """Header-only parse, used by list/search to avoid fetching bodies."""
    msg = email.message_from_bytes(raw)
    return {
        "message_id": decode_header_value(msg.get("Message-ID", "")),
        "subject": decode_header_value(msg.get("Subject", "")),
        "from": display_address(msg.get("From", "")),
        "date": decode_header_value(msg.get("Date", "")),
    }


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------

def _guess_type(path: Path) -> Tuple[str, str]:
    guessed, _ = mimetypes.guess_type(str(path))
    maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
    return maintype or "application", subtype or "octet-stream"


def _attach_files(message: EmailMessage, paths: Iterable[str], base_dir: Optional[Path]) -> List[str]:
    attached: List[str] = []
    for raw_path in paths:
        candidate = Path(str(raw_path)).expanduser()
        if not candidate.is_absolute() and base_dir is not None:
            candidate = (base_dir / candidate).resolve()
        if not candidate.is_file():
            raise MailError(f"attachment not found: {raw_path}")
        maintype, subtype = _guess_type(candidate)
        data = candidate.read_bytes()
        message.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=candidate.name,
        )
        attached.append(candidate.name)
    return attached


def build_message(
    *,
    sender: str,
    sender_name: str = "",
    to: Iterable[str],
    subject: str = "",
    text: str = "",
    html: str = "",
    cc: Iterable[str] = (),
    bcc: Iterable[str] = (),
    reply_to: str = "",
    in_reply_to: str = "",
    references: Iterable[str] = (),
    attachments: Iterable[str] = (),
    attachment_paths: Optional[Path] = None,
    date: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[EmailMessage, Dict[str, Any]]:
    """Build a MIME message and return ``(message, summary)``.

    Body assembly follows RFC 2046: ``multipart/alternative`` when both text
    and HTML are supplied, plain ``text/html`` for HTML-only, bare
    ``text/plain`` otherwise. Attachments wrap that in ``multipart/mixed``.
    """
    to_list = [a for a in (str(x).strip() for x in to) if a]
    cc_list = [a for a in (str(x).strip() for x in cc) if a]
    bcc_list = [a for a in (str(x).strip() for x in bcc) if a]
    attach_list = [a for a in (str(x).strip() for x in attachments) if a]

    if not to_list:
        raise MailError("at least one recipient is required")
    if not text and not html:
        raise MailError("either text or html body is required")

    message = EmailMessage()
    message["From"] = format_address(f"{sender_name} <{sender}>" if sender_name else sender)
    message["To"] = ", ".join(format_address(a) for a in to_list)
    if cc_list:
        message["Cc"] = ", ".join(format_address(a) for a in cc_list)
    if bcc_list:
        message["Bcc"] = ", ".join(format_address(a) for a in bcc_list)
    message["Subject"] = subject or ""
    message["Date"] = date or email.utils.formatdate(localtime=True)
    message["Message-ID"] = email.utils.make_msgid(domain=sender.rsplit("@", 1)[-1] or None)
    if reply_to:
        message["Reply-To"] = reply_to

    ref_list = [r for r in (str(x).strip() for x in references) if r]
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        if not ref_list:
            ref_list = [in_reply_to]
    if ref_list:
        message["References"] = " ".join(ref_list)

    for key, value in (extra_headers or {}).items():
        if value not in (None, "") and key not in message:
            message[key] = str(value)

    if text and html:
        message.set_content(text, subtype="plain", charset="utf-8")
        message.add_alternative(html, subtype="html", charset="utf-8")
    elif html:
        message.set_content(html, subtype="html", charset="utf-8")
    else:
        message.set_content(text, subtype="plain", charset="utf-8")

    names: List[str] = []
    if attach_list:
        names = _attach_files(message, attach_list, attachment_paths)

    summary = {
        "message_id": message["Message-ID"],
        "to": to_list,
        "cc": cc_list,
        "bcc": bcc_list,
        "subject": subject or "",
        "date": message["Date"],
        "format": "multipart/alternative" if (text and html) else ("text/html" if html else "text/plain"),
        "attachments": names,
    }
    return message, summary
