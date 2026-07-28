"""Configurable options catalog for Remote Analysis (documentation source of truth)."""

from __future__ import annotations

# Each entry: key, source, default, description
CONFIGURATION_CATALOG: list[dict[str, str]] = [
    # Portal singleton RemoteAnalysisSettings
    {"key": "guacamole_base_url", "source": "RemoteAnalysisSettings", "default": "", "description": "Public Guacamole URL (server-side redirects only)"},
    {"key": "guacamole_api_url", "source": "RemoteAnalysisSettings", "default": "", "description": "Internal Guacamole REST API; never returned to browsers"},
    {"key": "mock_guacamole", "source": "RemoteAnalysisSettings", "default": "True", "description": "Dev/test mock Guacamole; MUST be False in production"},
    {"key": "connection_timeout", "source": "RemoteAnalysisSettings", "default": "30s", "description": "Guacamole connection timeout"},
    {"key": "session_timeout", "source": "RemoteAnalysisSettings", "default": "120m", "description": "Absolute session lifetime"},
    {"key": "idle_timeout", "source": "RemoteAnalysisSettings", "default": "15m", "description": "Idle disconnect threshold"},
    {"key": "max_concurrent_sessions", "source": "RemoteAnalysisSettings", "default": "50", "description": "Global concurrent session cap"},
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
    # Code constants
    {"key": "HEARTBEAT_OFFLINE_SECONDS", "source": "constants.py", "default": "90", "description": "Mark agent offline after missed heartbeats"},
    {"key": "HEARTBEAT_STALE_SECONDS", "source": "constants.py", "default": "120", "description": "Stale heartbeat threshold"},
    {"key": "MIN_HEALTH_SCORE_FOR_ALLOCATION", "source": "constants.py", "default": "50", "description": "Minimum health score to allocate"},
    # Django / Celery env
    {"key": "CELERY_BROKER_URL", "source": "settings/REDIS_URL", "default": "redis://", "description": "Celery broker"},
    {"key": "CELERY_TASK_TIME_LIMIT", "source": "settings", "default": "300s", "description": "Hard task time limit"},
    {"key": "CELERY_TASK_SOFT_TIME_LIMIT", "source": "settings", "default": "60s", "description": "Soft task time limit"},
    {"key": "CACHES", "source": "settings local|production", "default": "LocMem|Redis", "description": "Django cache backend"},
    # Agent
    {"key": "PortalUrl", "source": "AgentOptions", "default": "(required)", "description": "Portal base URL for agent"},
    {"key": "HeartbeatSeconds", "source": "AgentOptions", "default": "30", "description": "Agent heartbeat interval"},
    {"key": "CommandPollSeconds", "source": "AgentOptions", "default": "15", "description": "Command poll interval"},
    {"key": "WorkspaceRoot", "source": "AgentOptions", "default": "(local path)", "description": "Agent local workspace root"},
    {"key": "LocalApiPort", "source": "AgentOptions", "default": "5088", "description": "Agent local diagnostic API port"},
]


def catalog_as_markdown() -> str:
    lines = ["| Key | Source | Default | Description |", "|-----|--------|---------|-------------|"]
    for row in CONFIGURATION_CATALOG:
        lines.append(f"| `{row['key']}` | {row['source']} | {row['default']} | {row['description']} |")
    return "\n".join(lines)
