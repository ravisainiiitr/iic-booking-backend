"""
Is a SRIC wallet recharge email genuine?

Mail reaching the portal mailbox carries no SPF / DKIM verdict today (no Authentication-Results header), so an
email counts as genuine when either

* a trusted mail server (configured authserv-id) recorded spf / dkim / dmarc = pass for the sender's domain, or
* it never crossed the public Internet: every Received hop came from a private (campus) address or a configured
  trusted range, and the campus gateway's marker header is present exactly once with the expected value.

Anything else is not auto-credited; its rows wait for the Main Administrator.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from email.message import Message
from email.utils import parseaddr

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_IPV6 = re.compile(r"\[(?:IPv6:)?([0-9a-fA-F:]{3,})\]")
_VERDICT = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-z]+)([^;]*)", re.I)
INTERNAL_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7")
)
_PROP = re.compile(r"\b(header\.d|header\.i|header\.from|smtp\.mailfrom|smtp\.helo)\s*=\s*([^\s;]+)", re.I)


@dataclass
class AuthResult:
    authenticated: bool
    verdict: str


def _lines(raw: str) -> list[str]:
    return [line.strip() for line in re.split(r"[\n,;]+", raw or "") if line.strip()]


def _domain(value: str) -> str:
    value = (value or "").strip().strip('"<>').lower()
    return value.rsplit("@", 1)[-1]


def _aligned(domain: str, sender_domain: str) -> bool:
    return bool(domain) and (domain == sender_domain or domain.endswith("." + sender_domain) or sender_domain.endswith("." + domain))


def authentication_results_pass(msg: Message, sender_domain: str, trusted_ids: list[str]) -> str:
    """'spf' / 'dkim' / 'dmarc' if a trusted Authentication-Results header passes for the sender domain."""
    trusted = {t.lower() for t in trusted_ids}
    if not trusted:
        return ""
    for header in msg.get_all("Authentication-Results") or []:
        flat = " ".join(str(header).split())
        authserv = flat.split(";", 1)[0].strip().split()[0].lower() if flat else ""
        if authserv not in trusted:
            continue
        for clause in flat.split(";")[1:]:
            m = _VERDICT.search(clause)
            if not m or m.group(2).lower() != "pass":
                continue
            props = {k.lower(): _domain(v) for k, v in _PROP.findall(clause)}
            if any(_aligned(d, sender_domain) for d in props.values()):
                return m.group(1).lower()
    return ""


def _hop_ips(received: str) -> list[str]:
    flat = " ".join(str(received).split())
    part = re.split(r"\bby\b", flat, maxsplit=1, flags=re.I)[0]
    return _IPV4.findall(part) + _IPV6.findall(part)


def internal_relay(msg: Message, trusted_ranges: list[str]) -> tuple[bool, str]:
    hops = msg.get_all("Received") or []
    if not hops:
        return False, "no Received headers"
    networks = []
    for cidr in trusted_ranges:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    for hop in hops:
        for raw in _hop_ips(hop):
            try:
                ip = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if any(ip.version == n.version and ip in n for n in INTERNAL_NETWORKS + tuple(networks)):
                continue
            return False, "relayed from a public address"
    return True, f"{len(hops)} internal hops"


def gateway_marker(msg: Message, header: str, expected: str) -> tuple[bool, str]:
    if not header:
        return True, "no marker required"
    values = msg.get_all(header) or []
    if len(values) != 1:
        return False, f"marker present {len(values)} times"
    if expected and str(values[0]).strip().lower() != expected.strip().lower():
        return False, "marker value differs"
    return True, "marker ok"


def check_message(msg: Message, config) -> AuthResult:
    sender = (getattr(config, "sender_email", "") or "").strip().lower()
    from_addr = parseaddr(msg.get("From") or "")[1].lower()
    if not sender or from_addr != sender:
        return AuthResult(False, "sender does not match")
    sender_domain = _domain(sender)
    return_path = parseaddr(msg.get("Return-Path") or "")[1].lower()
    if return_path and not _aligned(_domain(return_path), sender_domain):
        return AuthResult(False, "return-path domain differs")

    method = authentication_results_pass(msg, sender_domain, _lines(getattr(config, "trusted_authserv_ids", "")))
    if method:
        return AuthResult(True, f"authentication-results {method}=pass")
    if not getattr(config, "require_internal_relay", True):
        return AuthResult(False, "no trusted authentication result")
    relay_ok, relay_note = internal_relay(msg, _lines(getattr(config, "trusted_relay_ranges", "")))
    if not relay_ok:
        return AuthResult(False, relay_note)
    marker_ok, marker_note = gateway_marker(
        msg, (getattr(config, "gateway_marker_header", "") or "").strip(), getattr(config, "gateway_marker_value", "") or ""
    )
    if not marker_ok:
        return AuthResult(False, f"{relay_note}; {marker_note}")
    return AuthResult(True, f"internal relay ({relay_note}); {marker_note}")
