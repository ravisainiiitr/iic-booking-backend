"""Configurable options catalog for Remote Analysis (documentation source of truth)."""

from __future__ import annotations

# Each entry: key, source, default, description
CONFIGURATION_CATALOG: list[dict[str, str]] = [
    # Portal singleton RemoteAnalysisSettings
    {"key": "guacamole_base_url", "source": "RemoteAnalysisSettings", "default": "", "description": "Public Guacamole URL (server-side redirects only)"},
    {"key": "guacamole_api_url", "source": "RemoteAnalysisSettings", "default": "", "description": "Internal Guacamole REST API; never returned to browsers"},
    {"key": "mock_guacamole", "source": "RemoteAnalysisSettings", "default": "True", "description": "Dev/test mock Guacamole; MUST be False in production"},
    {"key": "transport_mode", "source": "RemoteAnalysisSettings", "default": "direct_rdp", "description": "direct_rdp | reverse_tunnel — how guacd reaches the Analysis PC"},
    {"key": "tunnel_gateway_admin_url", "source": "RemoteAnalysisSettings", "default": "", "description": "Internal Reverse Tunnel Gateway admin HTTP URL"},
    {"key": "tunnel_gateway_wss_url", "source": "RemoteAnalysisSettings", "default": "", "description": "Public WSS URL for agent reverse tunnels"},
    {"key": "tunnel_adapter_hostname", "source": "RemoteAnalysisSettings", "default": "reverse-tunnel-gateway", "description": "Hostname guacd dials for the TCP adapter"},
    {"key": "connection_timeout", "source": "RemoteAnalysisSettings", "default": "30s", "description": "Guacamole connection timeout"},
    {"key": "session_timeout", "source": "RemoteAnalysisSettings", "default": "120m", "description": "Absolute session lifetime"},
    {"key": "idle_timeout", "source": "RemoteAnalysisSettings", "default": "15m", "description": "Idle disconnect threshold"},
    {"key": "max_concurrent_sessions", "source": "RemoteAnalysisSettings", "default": "50", "description": "Global concurrent session cap"},
    {"key": "single_active_session_per_booking", "source": "RemoteAnalysisSettings", "default": "True", "description": "One open remote desktop session per booking"},
    {"key": "prepare_timeout_seconds", "source": "RemoteAnalysisSettings", "default": "120", "description": "Prepare workstation timeout"},
    {"key": "launch_token_lifetime_seconds", "source": "RemoteAnalysisSettings", "default": "90", "description": "One-time launch token TTL"},
    {"key": "bind_token_to_ip", "source": "RemoteAnalysisSettings", "default": "False", "description": "Bind launch tokens to client IP"},
    {"key": "workspace_root", "source": "RemoteAnalysisSettings", "default": "MEDIA/remote_analysis/workspaces", "description": "Portal workspace storage root"},
    {"key": "archive_root", "source": "RemoteAnalysisSettings", "default": "MEDIA/remote_analysis/archives", "description": "Workspace archive root"},
    {"key": "default_quota_gb", "source": "RemoteAnalysisSettings", "default": "50", "description": "Default workspace quota"},
    {"key": "retention_days", "source": "RemoteAnalysisSettings", "default": "90", "description": "Archived workspace retention"},
    {"key": "chunk_size_bytes", "source": "RemoteAnalysisSettings", "default": "5MB", "description": "Upload chunk size"},
    {"key": "maximum_upload_size", "source": "RemoteAnalysisSettings", "default": "2GB", "description": "Max upload size"},
    {"key": "maximum_download_size", "source": "RemoteAnalysisSettings", "default": "2GB", "description": "Max download size"},
    {"key": "version_history_limit", "source": "RemoteAnalysisSettings", "default": "20", "description": "File version history depth"},
    {"key": "virus_scanner", "source": "RemoteAnalysisSettings", "default": "noop", "description": "Scanner backend (noop|clamav future)"},
    {"key": "workspace_sync_mode", "source": "RemoteAnalysisSettings", "default": "end_of_session", "description": "end_of_session | interval automatic output sync"},
    {"key": "workspace_sync_interval_seconds", "source": "RemoteAnalysisSettings", "default": "300", "description": "Interval mode collect cadence"},
    {"key": "transfer_max_retries", "source": "RemoteAnalysisSettings", "default": "3", "description": "Agent transfer retry count"},
    {"key": "compression_enabled", "source": "RemoteAnalysisSettings", "default": "False", "description": "Optional gzip for large agent uploads"},
    {"key": "compression_min_bytes", "source": "RemoteAnalysisSettings", "default": "5MB", "description": "Compress uploads at or above this size"},
    {"key": "bandwidth_limit_kbps", "source": "RemoteAnalysisSettings", "default": "0", "description": "Advisory agent bandwidth cap; 0 unlimited"},
    # Code constants
    {"key": "HEARTBEAT_OFFLINE_SECONDS", "source": "constants.py", "default": "90", "description": "Mark agent offline after missed heartbeats"},
    {"key": "HEARTBEAT_STALE_SECONDS", "source": "constants.py", "default": "120", "description": "Stale heartbeat threshold"},
    {"key": "MIN_HEALTH_SCORE_FOR_ALLOCATION", "source": "constants.py", "default": "50", "description": "Minimum health score to allocate"},
    # Django / Celery env
    {"key": "CELERY_BROKER_URL", "source": "settings/REDIS_URL", "default": "redis://", "description": "Celery broker"},
    {"key": "CELERY_TASK_TIME_LIMIT", "source": "settings", "default": "300s", "description": "Hard task time limit"},
    {"key": "CELERY_TASK_SOFT_TIME_LIMIT", "source": "settings", "default": "60s", "description": "Soft task time limit"},
    {"key": "CACHES", "source": "settings local|production", "default": "LocMem|Redis", "description": "Django cache backend"},
    # Phase 2 Guacamole env overlays (applied via settings_env / sync_remote_analysis_settings)
    {"key": "RA_MOCK_GUACAMOLE", "source": "environ", "default": "", "description": "Override mock_guacamole (true|false); empty = use DB setting"},
    {"key": "RA_TRANSPORT", "source": "environ", "default": "", "description": "Override transport_mode (direct_rdp|reverse_tunnel)"},
    {"key": "RA_TUNNEL_GATEWAY_ADMIN_URL", "source": "environ", "default": "", "description": "Override tunnel_gateway_admin_url"},
    {"key": "RA_TUNNEL_GATEWAY_WSS_URL", "source": "environ", "default": "", "description": "Override tunnel_gateway_wss_url"},
    {"key": "RA_TUNNEL_ADAPTER_HOSTNAME", "source": "environ", "default": "", "description": "Override tunnel_adapter_hostname"},
    {"key": "RA_TUNNEL_TOKEN_SECRET", "source": "environ", "default": "", "description": "HMAC secret for tunnel tokens (shared with Gateway)"},
    {"key": "RA_TUNNEL_GATEWAY_ADMIN_KEY", "source": "environ", "default": "", "description": "Admin API key Portal→Gateway"},
    {"key": "RA_GUACAMOLE_BASE_URL", "source": "environ", "default": "", "description": "Override public Guacamole base URL"},
    {"key": "RA_GUACAMOLE_API_URL", "source": "environ", "default": "", "description": "Override internal Guacamole REST API URL"},
    {"key": "RA_GUACAMOLE_ADMIN_USERNAME", "source": "environ", "default": "", "description": "Override Guacamole admin username"},
    {"key": "RA_GUACAMOLE_ADMIN_PASSWORD", "source": "environ", "default": "", "description": "Override Guacamole admin password"},
    {"key": "RA_GUACAMOLE_DATA_SOURCE", "source": "environ", "default": "", "description": "Override Guacamole data source name"},
    {"key": "RA_GUACAMOLE_VERIFY_TLS", "source": "environ", "default": "", "description": "Override verify_tls for Guacamole REST (true|false)"},
    {"key": "RA_AGENT_ENROLLMENT_KEY", "source": "environ", "default": "", "description": "Shared secret required on POST /register/ when set; required for readiness when DEBUG=False"},
    {"key": "RA_APPLY_ENV_SETTINGS", "source": "environ", "default": "false", "description": "Persist RA_* overlays into DB on process start"},
    {"key": "DEFAULT_TOKEN_LIFETIME_DAYS", "source": "constants.py", "default": "90", "description": "Agent bearer token lifetime"},
    # Agent (RemoteAnalysisAgentOptions)
    {"key": "PortalBaseUrl", "source": "RemoteAnalysisAgentOptions", "default": "(required)", "description": "Portal origin for agent HTTPS calls"},
    {"key": "HeartbeatIntervalSeconds", "source": "RemoteAnalysisAgentOptions", "default": "30", "description": "Agent heartbeat interval"},
    {"key": "CommandPollIntervalSeconds", "source": "RemoteAnalysisAgentOptions", "default": "10", "description": "Command poll interval"},
    {"key": "HttpTimeoutSeconds", "source": "RemoteAnalysisAgentOptions", "default": "30", "description": "HTTP timeout to portal"},
    {"key": "MaxRetryAttempts", "source": "RemoteAnalysisAgentOptions", "default": "6", "description": "Transient portal HTTP retries"},
    {"key": "SessionWorkspaceRoot", "source": "RemoteAnalysisAgentOptions", "default": "ProgramData/.../Sessions", "description": "Agent local session workspace root"},
    {"key": "LocalHealthPort", "source": "RemoteAnalysisAgentOptions", "default": "5088", "description": "Loopback diagnostic health port (127.0.0.1 only); 0 disables"},
]


def catalog_as_markdown() -> str:
    lines = ["| Key | Source | Default | Description |", "|-----|--------|---------|-------------|"]
    for row in CONFIGURATION_CATALOG:
        lines.append(f"| `{row['key']}` | {row['source']} | {row['default']} | {row['description']} |")
    return "\n".join(lines)
