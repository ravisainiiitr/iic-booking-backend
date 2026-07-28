"""Control-plane exceptions with stable error codes."""

from __future__ import annotations


class SyncControlPlaneError(Exception):
    """Base error for Department Sync control-plane APIs."""

    code = "SYNC_ERROR"
    status_code = 400
    default_message = "Sync control-plane error."

    def __init__(self, message: str | None = None, *, code: str | None = None):
        self.message = message or self.default_message
        if code:
            self.code = code
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class AuthenticationFailedError(SyncControlPlaneError):
    code = "AUTH_FAILED"
    status_code = 401
    default_message = "Authentication failed."


class InvalidTokenError(AuthenticationFailedError):
    code = "INVALID_TOKEN"
    default_message = "Invalid or expired access token."


class UnknownAgentError(AuthenticationFailedError):
    code = "UNKNOWN_AGENT"
    default_message = "Unknown agent."


class DisabledAgentError(SyncControlPlaneError):
    code = "AGENT_DISABLED"
    status_code = 403
    default_message = "Agent is disabled."


class RevokedAgentError(SyncControlPlaneError):
    code = "AGENT_REVOKED"
    status_code = 403
    default_message = "Agent is revoked."


class EnrollmentFailedError(SyncControlPlaneError):
    code = "ENROLLMENT_FAILED"
    status_code = 400
    default_message = "Enrollment failed."


class InvalidEnrollmentSecretError(EnrollmentFailedError):
    code = "INVALID_ENROLLMENT_SECRET"
    default_message = "Invalid enrollment secret."


class InvalidLifecycleStateError(EnrollmentFailedError):
    code = "INVALID_LIFECYCLE_STATE"
    default_message = "Agent is not eligible for enrollment."


class InvalidConfigurationVersionError(SyncControlPlaneError):
    code = "INVALID_CONFIGURATION_VERSION"
    status_code = 400
    default_message = "Invalid configuration version."


class InvalidSchemaVersionError(SyncControlPlaneError):
    code = "INVALID_SCHEMA_VERSION"
    status_code = 400
    default_message = "Invalid schema version."
