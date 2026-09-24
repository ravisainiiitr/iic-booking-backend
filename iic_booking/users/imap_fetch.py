"""
IMAP helpers for fetching wallet recharge file from email attachments.
Uses stdlib imaplib and email. Admin-only usage via API.
"""
import imaplib
import email
import socket
from email.header import decode_header
from typing import Any, Dict, List, Optional, Tuple

# Max emails to return when listing (no subject filter)
LIST_EMAILS_MAX = 50
# When a subject filter is set, return all UID search hits up to this cap (newest first).
LIST_EMAILS_MAX_SUBJECT_FILTER = 5000


def _decode_mime_header(header: Optional[str]) -> str:
    if not header:
        return ""
    try:
        parts = decode_header(header)
        result = []
        for part, charset in parts:
            if isinstance(part, bytes):
                result.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                result.append(str(part))
        return " ".join(result).strip()
    except Exception:
        return str(header) if header else ""


def _extract_payload_text(part: email.message.Message) -> Optional[str]:
    """Get text content from a MIME part. Handles text/plain, text/csv, application/octet-stream with text content."""
    content_type = (part.get_content_type() or "").lower()
    if part.get_content_disposition() == "attachment":
        filename = part.get_filename() or ""
        name_lower = filename.lower()
        if not any(name_lower.endswith(ext) for ext in (".txt", ".csv", ".tsv")):
            if "text/" not in content_type and "application/octet-stream" not in content_type:
                return None
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return None
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    except Exception:
        return None


def _wallet_like_attachment_filename(filename: str) -> bool:
    n = (filename or "").lower().strip()
    return any(n.endswith(ext) for ext in (".txt", ".csv", ".tsv"))


def _get_first_text_attachment(msg: email.message.Message) -> Optional[Tuple[str, str]]:
    """
    Returns (filename_or_label, content_str) for the best part to parse.

    Prefer real attachments over inline body: many mails have a short text/html or text/plain
    body *before* wallet .txt parts in walk order; returning that body caused empty parses while
    choosing the named .txt attachment worked.
    """
    attachments: List[Tuple[email.message.Message, str]] = []
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            fn = _decode_mime_header(part.get_filename()) or ""
            attachments.append((part, fn))

    # 1) Named like wallet export first (same as user clicking the .txt in the UI)
    for part, filename in attachments:
        if _wallet_like_attachment_filename(filename):
            text = _decode_attachment_part_to_text(part)
            if text and len(text.strip()) > 0:
                return (filename or "attachment.txt", text)

    # 2) Any other attachment that decodes to non-empty text
    for part, filename in attachments:
        text = _decode_attachment_part_to_text(part)
        if text and len(text.strip()) > 0:
            return (filename or "attachment.txt", text)

    # 3) Inline body only if there were no usable attachments (legacy / pasted-in table)
    for part in msg.walk():
        if part.get_content_maintype() == "text" and part.get_content_disposition() != "attachment":
            text = _extract_payload_text(part)
            if text and len(text.strip()) > 0:
                return ("body", text)
    return None


def _get_attachment_part_at_index(
    msg: email.message.Message, attachment_index: int
) -> Optional[email.message.Message]:
    """Return the MIME part for the Nth attachment (0-based among disposition=attachment parts)."""
    idx = 0
    for part in msg.walk():
        if part.get_content_disposition() != "attachment":
            continue
        if idx == attachment_index:
            return part
        idx += 1
    return None


def _get_attachment_by_index(msg: email.message.Message, attachment_index: int) -> Optional[Tuple[bytes, str]]:
    """Get attachment at 0-based index (among parts with disposition=attachment). Returns (raw_bytes, filename) or None."""
    part = _get_attachment_part_at_index(msg, attachment_index)
    if not part:
        return None
    try:
        payload = part.get_payload(decode=True)
        filename = _decode_mime_header(part.get_filename()) or "attachment"
        return (payload or b""), filename
    except Exception:
        return None


def _decode_attachment_part_to_text(part: email.message.Message) -> Optional[str]:
    """
    Decode attachment part to str for parsing. Prefer same rules as _extract_payload_text (MIME charset);
    fall back to utf-8-sig / latin-1 / cp1252 so indexed fetches match 'first attachment' behavior.
    """
    text = _extract_payload_text(part)
    if text is not None and len(text.strip()) > 0:
        return text
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if payload is None:
        return None
    if len(payload) >= 2 and payload[:2] == b"\xff\xfe":
        try:
            return payload.decode("utf-16-le")
        except Exception:
            pass
    if len(payload) >= 2 and payload[:2] == b"\xfe\xff":
        try:
            return payload.decode("utf-16-be")
        except Exception:
            pass
    charset = (part.get_content_charset() or "").strip()
    if charset:
        try:
            return payload.decode(charset, errors="replace")
        except Exception:
            pass
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return payload.decode(enc)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _list_attachments(msg: email.message.Message) -> List[Dict[str, Any]]:
    """Return list of { index, filename, size } for each attachment."""
    result = []
    for idx, part in enumerate(msg.walk()):
        if part.get_content_disposition() != "attachment":
            continue
        filename = _decode_mime_header(part.get_filename()) or "attachment"
        size = None
        try:
            payload = part.get_payload(decode=True)
            if payload is not None:
                size = len(payload)
        except Exception:
            pass
        result.append({"index": len(result), "filename": filename, "size": size})
    return result


def connect_imap(
    host: str,
    port: int,
    use_ssl: bool,
    email_address: str,
    password: str,
) -> imaplib.IMAP4:
    conn = None
    try:
        if use_ssl:
            conn = imaplib.IMAP4_SSL(host, port=port)
        else:
            conn = imaplib.IMAP4(host, port=port)
    except (OSError, socket.timeout, socket.error) as e:
        err_str = str(e).strip()
        winerr = getattr(e, "winerror", None)
        if winerr == 10060 or "10060" in err_str or "timed out" in err_str.lower() or "timeout" in err_str.lower():
            raise ValueError(
                "Connection timed out. Check IMAP host and port, and that your network/firewall allows outbound connections to this server (e.g. port 993 for SSL, 143 for non-SSL)."
            ) from e
        if winerr == 10061 or "10061" in err_str or "connection refused" in err_str.lower():
            raise ValueError(
                "Connection refused. Check IMAP host and port (e.g. 993 for SSL, 143 for non-SSL)."
            ) from e
        raise ValueError(f"Cannot connect to {host}:{port}. {err_str}") from e
    except Exception as e:
        err_str = str(e).strip()
        if "timed out" in err_str.lower() or "timeout" in err_str.lower() or "10060" in err_str:
            raise ValueError(
                "Connection timed out. Check IMAP host and port, and that your network allows outbound connections."
            ) from e
        raise

    try:
        conn.login(email_address, password)
    except Exception as e:
        msg = getattr(e, "args", [None])
        if msg:
            raw = msg[0]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            text = str(raw) if raw else str(e)
            if "[AUTHENTICATIONFAILED]" in text.upper() or "invalid credentials" in text.lower():
                raise ValueError("Invalid email or password.") from e
        raise
    return conn


def list_emails(
    host: str,
    port: int,
    use_ssl: bool,
    email_address: str,
    password: str,
    folder: str = "INBOX",
    sender_filter: Optional[str] = None,
    subject_filter: Optional[str] = None,
    max_results: int = LIST_EMAILS_MAX,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    List emails (last N when unfiltered). Optional filter by sender and/or subject (substring match).
    If subject_filter is set, returns all UID search hits up to LIST_EMAILS_MAX_SUBJECT_FILTER (newest first),
    not only the last LIST_EMAILS_MAX messages.
    Returns (list of { uid, subject, from_addr, date }, error_message).
    """
    try:
        conn = connect_imap(host, port, use_ssl, email_address, password)
    except Exception as e:
        return [], str(e)
    try:
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            return [], f"Could not select folder: {folder}"
        search_criteria = "ALL"
        if sender_filter or subject_filter:
            parts = []
            if sender_filter:
                # Escape " and \ for IMAP search string
                s = sender_filter.strip().replace("\\", "\\\\").replace('"', '\\"')
                parts.append(f'FROM "{s}"')
            if subject_filter:
                s = subject_filter.strip().replace("\\", "\\\\").replace('"', '\\"')
                parts.append(f'SUBJECT "{s}"')
            if parts:
                search_criteria = " ".join(parts)
        # UID SEARCH so identifiers stay stable across separate connections (Fetch runs after List).
        status, data = conn.uid("search", None, search_criteria)
        if status != "OK":
            return [], "Search failed"
        id_list = data[0].split()
        if not id_list:
            return [], None
        # With a subject filter, return all matching messages (capped), not only the last 50 UIDs.
        if subject_filter and subject_filter.strip():
            cap = LIST_EMAILS_MAX_SUBJECT_FILTER
            if len(id_list) > cap:
                id_list = id_list[-cap:]
        else:
            id_list = id_list[-max_results:]
        id_list.reverse()
        result = []
        for uid in id_list:
            try:
                status, msg_data = conn.uid("fetch", uid, "(RFC822.HEADER)")
                if status != "OK" or not msg_data:
                    continue
                raw = msg_data[0]
                if isinstance(raw, tuple) and len(raw) >= 2:
                    header_bytes = raw[1]
                else:
                    continue
                msg = email.message_from_bytes(header_bytes)
                result.append({
                    "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                    "subject": _decode_mime_header(msg.get("Subject")),
                    "from_addr": _decode_mime_header(msg.get("From")),
                    "date": _decode_mime_header(msg.get("Date")),
                })
            except Exception:
                continue
        return result, None
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def delete_email_by_uid(
    host: str,
    port: int,
    use_ssl: bool,
    email_address: str,
    password: str,
    folder: str,
    email_uid: str,
) -> Tuple[bool, Optional[str]]:
    """
    Permanently remove one message by IMAP UID (mark \\Deleted and EXPUNGE).
    Server must allow deletion in the selected folder (not read-only).
    Returns (success, error_message).
    """
    conn = None
    try:
        conn = connect_imap(host, port, use_ssl, email_address, password)
    except Exception as e:
        return False, str(e)
    try:
        status, _ = conn.select(folder)
        if status != "OK":
            return False, f"Could not select folder: {folder}"
        uid_str = email_uid.decode() if isinstance(email_uid, bytes) else str(email_uid).strip()
        if not uid_str:
            return False, "Invalid UID"
        typ, _ = conn.uid("STORE", uid_str, "+FLAGS", "\\Deleted")
        if typ != "OK":
            return False, "Failed to mark message for deletion"
        try:
            conn.expunge()
        except Exception as exp_err:
            return False, f"Expunge failed: {exp_err}"
        return True, None
    except Exception as e:
        return False, str(e)
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


def fetch_email_attachment(
    host: str,
    port: int,
    use_ssl: bool,
    email_address: str,
    password: str,
    email_uid: str,
    folder: str = "INBOX",
    attachment_index: Optional[int] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Fetch one email by UID and return attachment content as text for parsing.
    If attachment_index is set, use that attachment (0-based among attachments); else first text/csv.
    Returns (content_str, filename_or_none, error_message).
    """
    try:
        conn = connect_imap(host, port, use_ssl, email_address, password)
    except Exception as e:
        return None, None, str(e)
    try:
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            return None, None, f"Could not select folder: {folder}"
        uid_bin = email_uid.encode() if isinstance(email_uid, str) else str(email_uid).encode()
        status, msg_data = conn.uid("fetch", uid_bin, "(RFC822)")
        if status != "OK" or not msg_data:
            return None, None, "Email not found or could not fetch"
        raw = msg_data[0]
        if isinstance(raw, tuple) and len(raw) >= 2:
            body = raw[1]
        else:
            return None, None, "Invalid message data"
        msg = email.message_from_bytes(body)

        if attachment_index is not None:
            part = _get_attachment_part_at_index(msg, attachment_index)
            if not part:
                return None, None, f"Attachment index {attachment_index} not found"
            filename = _decode_mime_header(part.get_filename()) or "attachment"
            content = _decode_attachment_part_to_text(part)
            if not content or not str(content).strip():
                return None, None, f"Attachment {attachment_index} is empty or could not be decoded as text"
            return content, filename, None

        pair = _get_first_text_attachment(msg)
        if not pair:
            return None, None, "No text/csv attachment found in this email"
        filename, content = pair
        return content, filename, None
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def list_attachments_for_email(
    host: str,
    port: int,
    use_ssl: bool,
    email_address: str,
    password: str,
    email_uid: str,
    folder: str = "INBOX",
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Fetch one email by UID and return list of attachments: [ { index, filename, size } ]."""
    try:
        conn = connect_imap(host, port, use_ssl, email_address, password)
    except Exception as e:
        return [], str(e)
    try:
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            return [], f"Could not select folder: {folder}"
        uid_bin = email_uid.encode() if isinstance(email_uid, str) else str(email_uid).encode()
        status, msg_data = conn.uid("fetch", uid_bin, "(RFC822)")
        if status != "OK" or not msg_data:
            return [], "Email not found or could not fetch"
        raw = msg_data[0]
        if isinstance(raw, tuple) and len(raw) >= 2:
            body = raw[1]
        else:
            return [], "Invalid message data"
        msg = email.message_from_bytes(body)
        return _list_attachments(msg), None
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def get_attachment_content(
    host: str,
    port: int,
    use_ssl: bool,
    email_address: str,
    password: str,
    email_uid: str,
    attachment_index: int,
    folder: str = "INBOX",
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Fetch one email by UID and return attachment at index as raw bytes. Returns (content_bytes, filename, error)."""
    try:
        conn = connect_imap(host, port, use_ssl, email_address, password)
    except Exception as e:
        return None, None, str(e)
    try:
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            return None, None, f"Could not select folder: {folder}"
        uid_bin = email_uid.encode() if isinstance(email_uid, str) else str(email_uid).encode()
        status, msg_data = conn.uid("fetch", uid_bin, "(RFC822)")
        if status != "OK" or not msg_data:
            return None, None, "Email not found or could not fetch"
        raw = msg_data[0]
        if isinstance(raw, tuple) and len(raw) >= 2:
            body = raw[1]
        else:
            return None, None, "Invalid message data"
        msg = email.message_from_bytes(body)
        pair = _get_attachment_by_index(msg, attachment_index)
        if not pair:
            return None, None, f"Attachment index {attachment_index} not found"
        payload, filename = pair
        return payload, filename, None
    finally:
        try:
            conn.logout()
        except Exception:
            pass
