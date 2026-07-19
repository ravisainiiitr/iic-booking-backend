"""
IMAP email reader service.

Reads email from a mailbox using IMAP over SSL (e.g. imap.iitr.ac.in:993).
Credentials and server settings come from Django settings (IMAP_*).
"""

import email
import imaplib
import logging
import socket
from email.utils import parsedate_to_datetime
from typing import Any, Iterator, List, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# Seconds to wait for TCP connect / IMAP handshake (avoid hanging until OS WinError 10060).
IMAP_CONNECT_TIMEOUT_SECONDS = int(getattr(settings, "IMAP_CONNECT_TIMEOUT", 20) or 20)


def _friendly_imap_connect_error(host: str, port: int, exc: BaseException) -> ValueError:
    err_str = str(exc).strip()
    winerr = getattr(exc, "winerror", None)
    if (
        winerr == 10060
        or "10060" in err_str
        or "timed out" in err_str.lower()
        or "timeout" in err_str.lower()
    ):
        return ValueError(
            f"IMAP connection to {host}:{port} timed out. "
            "Check IMAP_HOST / IMAP_PORT in server settings, that the mail server is reachable "
            "from this machine, and that the firewall allows outbound TCP "
            "(typically 993 for SSL or 143 without SSL)."
        )
    if winerr == 10061 or "10061" in err_str or "connection refused" in err_str.lower():
        return ValueError(
            f"IMAP connection to {host}:{port} was refused. "
            "Verify host/port and SSL settings (IMAP_USE_SSL)."
        )
    if "getaddrinfo" in err_str.lower() or "name or service not known" in err_str.lower():
        return ValueError(
            f"IMAP host '{host}' could not be resolved. Check IMAP_HOST spelling/DNS."
        )
    return ValueError(f"Cannot connect to IMAP {host}:{port}. {err_str}")


class IMAPEmailReader:
    """
    Read emails from an IMAP mailbox (SSL/TLS, normal password auth).

    Uses settings: IMAP_HOST, IMAP_PORT, IMAP_USE_SSL, IMAP_USER, IMAP_PASSWORD, IMAP_MAILBOX.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        use_ssl: Optional[bool] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        mailbox: Optional[str] = None,
    ):
        self.host = host or getattr(settings, "IMAP_HOST", "imap.iitr.ac.in")
        self.port = port if port is not None else getattr(settings, "IMAP_PORT", 993)
        self.use_ssl = use_ssl if use_ssl is not None else getattr(settings, "IMAP_USE_SSL", True)
        self.user = user or getattr(settings, "IMAP_USER", "")
        self.password = password or getattr(settings, "IMAP_PASSWORD", "")
        self.mailbox = mailbox or getattr(settings, "IMAP_MAILBOX", "INBOX")
        self._connection: Optional[imaplib.IMAP4] = None

    def _connect(self) -> imaplib.IMAP4:
        """Establish and return IMAP connection (SSL on port 993 or plain on 143)."""
        if not self.user or not self.password:
            raise ValueError(
                "IMAP credentials are not configured. Set IMAP_USER and IMAP_PASSWORD "
                "(and IMAP_HOST if needed) in the server environment."
            )
        if self._connection is not None:
            try:
                self._connection.noop()
                return self._connection
            except Exception:
                self._connection = None
        try:
            # Bound connect so Windows does not sit until WinError 10060 (~20–60s+).
            socket.setdefaulttimeout(IMAP_CONNECT_TIMEOUT_SECONDS)
            try:
                if self.use_ssl:
                    self._connection = imaplib.IMAP4_SSL(self.host, self.port)
                else:
                    self._connection = imaplib.IMAP4(self.host, self.port)
            finally:
                socket.setdefaulttimeout(None)
        except (OSError, socket.timeout, socket.error, TimeoutError) as e:
            logger.exception("IMAP connect failed to %s:%s", self.host, self.port)
            raise _friendly_imap_connect_error(self.host, self.port, e) from e
        except Exception as e:
            logger.exception("IMAP connect unexpected error to %s:%s", self.host, self.port)
            raise _friendly_imap_connect_error(self.host, self.port, e) from e
        try:
            self._connection.login(self.user, self.password)
        except imaplib.IMAP4.error as e:
            self.disconnect()
            raise ValueError(
                "IMAP login failed. Check IMAP_USER and IMAP_PASSWORD "
                "(and whether the account requires an app password)."
            ) from e
        logger.info("IMAP connected to %s:%s as %s", self.host, self.port, self.user)
        return self._connection

    def disconnect(self) -> None:
        """Close the IMAP connection."""
        if self._connection:
            try:
                self._connection.logout()
            except Exception as e:
                logger.debug("IMAP logout: %s", e)
            self._connection = None

    def __enter__(self) -> "IMAPEmailReader":
        self._connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.disconnect()

    def list_folders(self) -> List[str]:
        """Return list of folder names (e.g. INBOX, Sent)."""
        conn = self._connect()
        status, data = conn.list()
        if status != "OK":
            raise RuntimeError(f"IMAP LIST failed: {data}")
        folders = []
        for item in data or []:
            if isinstance(item, bytes):
                item = item.decode("utf-8", errors="replace")
            parts = item.split(' "/" ')
            if len(parts) >= 2:
                name = parts[-1].strip().strip('"')
                folders.append(name)
            else:
                folders.append(item.strip())
        return folders

    def list_folders_with_counts(self) -> List[dict]:
        """Return list of { name, count } for each folder (count = message count)."""
        names = self.list_folders()
        conn = self._connect()
        result = []
        for name in names:
            try:
                status, data = conn.select(name, readonly=True)
                if status == "OK" and data:
                    count = int(data[0])
                else:
                    count = 0
            except Exception:
                count = 0
            result.append({"name": name, "count": count})
        return result

    def select(self, mailbox: Optional[str] = None) -> int:
        """Select a mailbox; returns message count."""
        conn = self._connect()
        folder = mailbox or self.mailbox
        status, data = conn.select(folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"IMAP SELECT {folder} failed: {data}")
        return int(data[0]) if data else 0

    def _parse_message(self, msg: email.message.Message) -> dict:
        """Turn an email.message.Message into a simple dict."""
        subject = ""
        from_addr = ""
        date_str = ""
        for key in ("Subject", "From", "Date"):
            val = msg.get(key)
            if val:
                if isinstance(val, bytes):
                    val = val.decode("utf-8", errors="replace")
                decoded = email.header.decode_header(val)
                parts = []
                for part, enc in decoded:
                    if isinstance(part, bytes):
                        part = part.decode(enc or "utf-8", errors="replace")
                    parts.append(str(part))
                val = " ".join(parts)
            if key == "Subject":
                subject = val or ""
            elif key == "From":
                from_addr = val or ""
            elif key == "Date":
                date_str = val or ""

        date_obj = None
        if date_str:
            try:
                date_obj = parsedate_to_datetime(date_str)
            except Exception:
                pass

        body_plain = ""
        body_html = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body_plain = payload.decode(charset, errors="replace")
                elif ctype == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body_html = payload.decode(charset, errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
                if msg.get_content_type() == "text/html":
                    body_html = text
                else:
                    body_plain = text

        return {
            "subject": subject,
            "from": from_addr,
            "date": date_obj,
            "date_raw": date_str,
            "body_plain": body_plain,
            "body_html": body_html,
        }

    def fetch_emails(
        self,
        mailbox: Optional[str] = None,
        since: Optional[str] = None,
        max_count: Optional[int] = None,
        mark_seen: bool = False,
    ) -> List[dict]:
        """
        Fetch emails from the given mailbox.

        Args:
            mailbox: Folder name (default: IMAP_MAILBOX / INBOX).
            since: IMAP date criterion, e.g. "01-Jan-2025".
            max_count: Maximum number of messages to fetch (newest first).
            mark_seen: If True, mark messages as read (SEEN).

        Returns:
            Tuple of (list of dicts with keys: uid, subject, from, date, date_raw, body_plain, body_html;
                     mailbox total message count).
        """
        conn = self._connect()
        folder = mailbox or self.mailbox

        # Some servers (e.g. Exchange) use "Inbox" instead of "INBOX"; try to resolve
        try:
            status, data = conn.select(folder, readonly=not mark_seen)
            if status != "OK":
                # Try listing folders and pick one that matches INBOX case-insensitively
                status_list, list_data = conn.list()
                if status_list == "OK" and list_data:
                    for item in list_data or []:
                        line = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
                        if ' "/" ' in line:
                            name = line.split(' "/" ')[-1].strip().strip('"')
                        else:
                            name = line.strip()
                        if name.upper() == "INBOX":
                            folder = name
                            status, data = conn.select(folder, readonly=not mark_seen)
                            break
        except Exception as e:
            logger.warning("IMAP select %s failed: %s", folder, e)
            raise RuntimeError(f"IMAP SELECT {folder} failed: {e}") from e

        if status != "OK":
            raise RuntimeError(f"IMAP SELECT {folder} failed: {data}")

        exists = int(data[0]) if data else 0
        logger.info("IMAP mailbox %s has %s message(s)", folder, exists)

        if exists == 0:
            return [], 0

        search_criteria = "ALL"
        if since:
            search_criteria = f'(SINCE "{since}")'

        status, data = conn.search(None, search_criteria)
        if status != "OK":
            raise RuntimeError(f"IMAP SEARCH failed: {data}")

        ids = (data[0] or b"").split()
        if not ids:
            # Some servers (e.g. Exchange) return empty SEARCH for "ALL"; fetch by sequence 1:exists
            logger.info("IMAP SEARCH returned 0 ids; fetching by sequence 1:%s", exists)
            ids = [str(i).encode("ascii") for i in range(1, exists + 1)]

        ids = list(reversed(ids))
        if max_count is not None:
            ids = ids[:max_count]

        result = []
        for id_bytes in ids:
            msg_id = id_bytes.decode("ascii") if isinstance(id_bytes, bytes) else str(id_bytes)
            status, msg_data = conn.fetch(id_bytes, "(RFC822)")
            if status != "OK" or not msg_data:
                continue
            part = msg_data[0]
            if isinstance(part, tuple) and len(part) >= 2:
                raw = part[1]
                if isinstance(raw, bytes):
                    try:
                        msg = email.message_from_bytes(raw)
                    except Exception as e:
                        logger.warning("Parse email %s: %s", msg_id, e)
                        continue
                else:
                    continue
            else:
                continue
            parsed = self._parse_message(msg)
            parsed["uid"] = msg_id
            result.append(parsed)
            if mark_seen:
                conn.store(id_bytes, "+FLAGS", "\\Seen")

        return result, exists

    def iter_emails(
        self,
        mailbox: Optional[str] = None,
        since: Optional[str] = None,
        mark_seen: bool = False,
    ) -> Iterator[dict]:
        """Yield emails one by one (same structure as fetch_emails)."""
        emails, _ = self.fetch_emails(mailbox=mailbox, since=since, mark_seen=mark_seen)
        for msg in emails:
            yield msg


def get_imap_reader(
    host: Optional[str] = None,
    port: Optional[int] = None,
    use_ssl: Optional[bool] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    mailbox: Optional[str] = None,
) -> IMAPEmailReader:
    """Factory: return an IMAPEmailReader using settings (and optional overrides)."""
    return IMAPEmailReader(
        host=host,
        port=port,
        use_ssl=use_ssl,
        user=user,
        password=password,
        mailbox=mailbox,
    )
