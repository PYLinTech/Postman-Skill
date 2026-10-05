---
name: postman
description: Read, search, send and manage email over IMAP/SMTP. Use when the user wants to check their inbox, find or read specific messages, draft or send mail, reply or forward, change read or starred state, move or delete messages, or save attachments. Supports QQ, NetEase, Gmail, Outlook, Yahoo, Sohu, Sina, Aliyun and any custom IMAP/SMTP server, and reads and sends HTML mail correctly. Standard library only, no dependencies.
license: MIT
allowed-tools:
  - Bash
  - Read
  - Write
metadata:
  version: "1.0.0"
  python: ">=3.8"
---

# Email

IMAP/SMTP from the command line. Standard library only — nothing to install.

## Setup

Credentials are required. Run `check` first; if it reports a configuration
error, ask the user for an address and an **app-specific password** (most
providers reject the normal account password for IMAP/SMTP).

| Source | How |
| --- | --- |
| File | `--config mail.json`; see `resources/config.example.json`. Multi-account files use an `accounts` map plus `--account name` |
| Env | `MAIL_ADDRESS`, `MAIL_PASSWORD`, optionally `MAIL_PROVIDER`, `MAIL_SENDER_NAME`, `MAIL_CONFIG` |

If neither is set, ask the user — never guess. The password is never printed
back in any output.

## Running

Set the path once, then use `$MAIL`:

```bash
MAIL=/absolute/path/to/this/skill/scripts/mail.py
python "$MAIL" check
```

Do not `cd` into the skill directory; it may be reset between runs. Output is
human-readable; add `--json` for structured output, or pass long HTML via
`python "$MAIL" json '{"command":"send","to":["a@b.com"],"html":"<p>hi</p>"}'`
when shell quoting gets awkward. Every command takes `--help`. Allow a bash
timeout of at least 120 seconds (TLS handshakes are slow on some providers) and
run these synchronously.

## Commands

| Command | Purpose |
| --- | --- |
| `check` | Verify credentials, list folders, unread count |
| `selftest` | Verify this build; `--live` also probes the mailbox |
| `folders` | List folders with special-use flags |
| `search` | Find messages — never changes the read flag |
| `read` | Full bodies, HTML, attachment listings |
| `send` / `draft` | Send over SMTP, or save to Drafts |
| `flag` | read/unread, star, and other flags |
| `move` / `delete` | Reorganise, with Trash fallback |
| `download` | Save attachments |

### Search and read

```bash
python "$MAIL" search --unread --limit 10
python "$MAIL" search --query 'FROM "boss@x.com" SINCE 2026-09-01'
python "$MAIL" search --gmail-raw 'has:attachment larger:5M'   # Gmail only
python "$MAIL" read --uid 12345
python "$MAIL" read --latest 3
python "$MAIL" download --uid 12345 --dir ~/Downloads
```

`--query` takes raw IMAP criteria; `--gmail-raw` takes Gmail's native syntax.

Common options: `-f/--folder` applies to every folder-scoped command
(`check`, `search`, `read`, `flag`, `move`, `delete`, `download`), defaulting to
`INBOX`; `draft` uses the same flag but defaults to `Drafts`. `search` also
takes `--unread`, `--flagged`, `--has-attachment`, `--since` / `--before`
(YYYY-MM-DD), `--limit` and `--offset` for paging; `download` takes `--name` to
save one attachment by name. Folders accept aliases (`inbox`, `sent`, `drafts`,
`trash`, `junk`, `archive`) and localised names such as `已发送`.

Search returns **UIDs**, stable even when other clients move mail — use them for
every follow-up. `read` reports `text_source`: `plain`, `html` (converted to
readable text), or `empty`. Reading never alters state unless `--mark-read` is
given. Bodies are capped at 20 000 characters (`--max-chars`, `0` disables) and
the output says when it truncated; pass `--html` only if you need the raw part,
since the extracted text already covers its content.

### Send

```bash
python "$MAIL" send --to a@b.com --subject 'Hi' --text 'Hello'
python "$MAIL" send --to a@b.com --cc c@d.com --subject 'Q3' \
    --html-file report.html --attach ./q3.pdf
python "$MAIL" send --to a@b.com --subject 'Re: x' --text 'y' \
    --in-reply-to '<abc@qq.com>'
```

Both `--text` and `--html` produce `multipart/alternative` — what recipients'
clients expect. Prefer `--html-file` / `--text-file` for long bodies. `--bcc`
recipients stay out of the visible To and Cc headers.

**Always show the user the resolved recipients, subject and body and get an
explicit go-ahead before sending.** It cannot be undone. Use `--dry-run` to
validate and preview without delivering.

### Organise

```bash
python "$MAIL" draft --to a@b.com --subject 'Idea' --text '...'
python "$MAIL" flag --uid 12345 --flags unread      # clears the Seen flag
python "$MAIL" flag --uid 12345 --flags star --mode remove
python "$MAIL" move --uid 12345 --to-folder archive
python "$MAIL" delete --uid 12345                   # Trash when present
```

Flag names: `read`, `unread`, `star`, `answered`, `deleted`, `draft`. `read`
and `unread` name a desired state and invert the Seen flag; `--mode remove`
flips them. `--permanent` on `delete` is irreversible — confirm first and prefer
the Trash default.

## Providers

Servers derive from the address domain; `provider` in the config file or
`MAIL_PROVIDER` overrides that.

| Domain | IMAP | SMTP |
| --- | --- | --- |
| `qq.com`, `foxmail.com` | `imap.qq.com:993` | `smtp.qq.com:587` |
| `163.com` / `126.com` / `yeah.net` | each its own host | each its own host |
| `gmail.com` | `imap.gmail.com:993` | `smtp.gmail.com:587` |
| `outlook.com`, `hotmail.com`, `live.*` | `outlook.office365.com:993` | `smtp.office365.com:587` |
| `yahoo.*`, `ymail.com` | `imap.mail.yahoo.com:993` | `smtp.mail.yahoo.com:587` |
| `sohu.com` | `imap.sohu.com:993` | `smtp.sohu.com:465` |
| `sina.*` | `imap.sina.com:993` | `smtp.sina.com:465` |
| `aliyun.com` | `imap.aliyun.com:993` | `smtp.aliyun.com:465` |

Anything else: set `imap_host` / `imap_port` / `smtp_host` / `smtp_port`.
Transport follows the port — 465 implicit TLS, 587 STARTTLS, otherwise plain —
and `smtp_security` pins it explicitly. NetEase and QQ require an RFC 2971
client ID before login; handled automatically.

Every provider needs an **app-specific password** with IMAP/SMTP enabled. A
login failure almost always means one of those is missing.

## Troubleshooting

| Message | Cause |
| --- | --- |
| `no mailbox address configured` | Credentials absent — ask the user |
| `IMAP login failed` / `SMTP authentication failed` | Wrong password, or an app password is required |
| `folder 'x' not found` | Run `folders`; localised names resolve automatically |
| TLS certificate verification failed | Corporate interception; `verify_ssl: false` only if the user accepts the risk |

## Notes

- `python "$MAIL" selftest` runs 74 offline checks — header decoding, HTML
  conversion, MIME assembly, server resolution, flag planning, filename safety —
  with no network or credentials. `--live` adds read-only IMAP probes against
  the configured account. Run `selftest` first when something behaves oddly: it
  separates "this build is broken" from "the server refused us".
- The password is never echoed; `check` reports `***`. Prefer `MAIL_PASSWORD`
  over a command argument, which can land in shell history.
- `search` and `read` leave read state alone unless `--mark-read` is passed.
- `move` uses COPY + EXPUNGE, which is not UID-scoped: anything else already
  flagged deleted in that folder goes too. Standard IMAP behaviour.
