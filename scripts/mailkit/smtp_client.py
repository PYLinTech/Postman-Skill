"""SMTP delivery.

Transport mode follows the port (465 implicit TLS, 587 STARTTLS, otherwise
plain) and can be pinned per account. The password is only ever passed to
``login``; it is never logged or included in error messages.
"""

from __future__ import annotations

import smtplib
import ssl
from typing import Any, Dict, Optional

from .config import smtp_security_for_port
from .mime import MailError


def _ssl_context(verify: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _describe(host: str, port: int, mode: str) -> str:
    return f"{host}:{port} ({mode})"


def send_message(
    config: Dict[str, Any],
    message: Any,
    *,
    recipients: Optional[list] = None,
) -> Dict[str, Any]:
    """Deliver *message*; returns a delivery summary.

    ``recipients`` defaults to every envelope recipient in the message (To, Cc
    and Bcc) so Bcc is not silently dropped.
    """
    host = str(config["smtp_host"])
    port = int(config["smtp_port"])
    mode = smtp_security_for_port(port, config.get("smtp_security"))
    timeout = int(config.get("timeout", 30))
    verify = bool(config.get("verify_ssl", True))
    address = str(config["address"])
    password = str(config.get("password", ""))
    context = _ssl_context(verify)

    targets = recipients if recipients else _envelope_recipients(message)
    if not targets:
        raise MailError("no recipients to deliver to")

    smtp: Optional[smtplib.SMTP] = None
    try:
        if mode == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            smtp = smtplib.SMTP(host, port, timeout=timeout)
            if mode == "starttls":
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()

        # A fresh EHLO after STARTTLS is mandatory; some servers only advertise
        # AUTH afterwards.
        smtp.ehlo()
        try:
            smtp.login(address, password)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError(
                f"SMTP authentication failed for {address} via {_describe(host, port, mode)}. "
                "Most providers require an app-specific password instead of the "
                f"account password, and some require SMTP authorization to be "
                f"enabled first. ({exc.smtp_code} {exc.smtp_error!r})"
            ) from exc
        except smtplib.SMTPException as exc:
            raise MailError(
                f"SMTP login failed for {address} via {_describe(host, port, mode)}: {exc}"
            ) from exc

        refused = smtp.send_message(message, from_addr=str(config.get("send_as") or address),
                                   to_addrs=targets)
        if refused:
            raise MailError(f"recipients refused by the server: {sorted(refused)}")
    except ssl.SSLCertVerificationError as exc:
        raise MailError(
            f"TLS certificate verification failed for {host}:{port}. Use a valid "
            "certificate, or set verify_ssl=false if you accept the risk."
        ) from exc
    except smtplib.SMTPResponseException as exc:
        raise MailError(
            f"server rejected the message ({exc.smtp_code} {exc.smtp_error!r}) "
            f"via {_describe(host, port, mode)}"
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        if isinstance(exc, MailError):
            raise
        raise MailError(f"cannot send via {_describe(host, port, mode)}: {exc}") from exc
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                try:
                    smtp.close()
                except Exception:
                    pass

    return {
        "sent": True,
        "from": address,
        "recipients": list(targets),
        "transport": mode,
        "server": f"{host}:{port}",
    }


def _envelope_recipients(message: Any) -> list:
    import email.utils

    out = []
    for header in ("To", "Cc", "Bcc"):
        for _name, addr in email.utils.getaddresses(message.get_all(header, [])):
            if addr:
                out.append(addr)
    seen, unique = set(), []
    for addr in out:
        if addr.lower() not in seen:
            seen.add(addr.lower())
            unique.append(addr)
    return unique
