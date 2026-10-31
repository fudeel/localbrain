"""Failures a caller can act on. Each carries a stable machine-readable code."""


class BrainError(Exception):
    code = "brain_error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class InvalidRequest(BrainError):
    code = "invalid_request"


class Unauthorized(BrainError):
    code = "unauthorized"


class Forbidden(BrainError):
    code = "forbidden"


class NotFound(BrainError):
    code = "not_found"


class MethodNotAllowed(BrainError):
    code = "method_not_allowed"


class PayloadTooLarge(BrainError):
    code = "payload_too_large"


class Conflict(BrainError):
    code = "conflict"


class AgentNotInstalled(Conflict):
    code = "agent_not_installed"


class AgentNotMounted(Conflict):
    code = "agent_not_mounted"


class AgentIncompatible(Conflict):
    code = "agent_incompatible"


class CapabilityMissing(Conflict):
    code = "capability_missing"


class CloudAgentRefused(BrainError):
    """This brain only ever runs models on local hardware."""

    code = "cloud_agent_refused"


class RuntimeUnavailable(BrainError):
    code = "runtime_unavailable"


class RuntimeRequestFailed(BrainError):
    code = "runtime_error"


class InstallFailed(BrainError):
    code = "install_failed"


class JobCancelled(BrainError):
    code = "job_cancelled"
