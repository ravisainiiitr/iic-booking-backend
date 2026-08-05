"""Shared Reverse Tunnel pytest configuration (non-production secrets only)."""

from __future__ import annotations

# Deterministic non-production secret for TunnelTokenService under DEBUG=False.
TEST_RA_TUNNEL_TOKEN_SECRET = "test-ra-tunnel-secret"


def apply_ra_tunnel_token_secret(settings, monkeypatch, *, secret: str = TEST_RA_TUNNEL_TOKEN_SECRET) -> str:
    """Inject RA_TUNNEL_TOKEN_SECRET into env and Django settings for pytest."""
    monkeypatch.setenv("RA_TUNNEL_TOKEN_SECRET", secret)
    settings.RA_TUNNEL_TOKEN_SECRET = secret
    return secret
