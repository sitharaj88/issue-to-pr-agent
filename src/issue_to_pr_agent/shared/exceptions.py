class ConfigurationError(ValueError):
    """Raised when application settings are invalid."""


class PolicyError(ValueError):
    """Raised when a requested action violates an execution policy."""


class StorageError(RuntimeError):
    """Raised when persistent run storage cannot be accessed."""


class ExecutionError(RuntimeError):
    """Raised when a patch proposal cannot be executed safely."""


class DeliveryError(RuntimeError):
    """Raised when a delivery workflow cannot be completed safely."""


class ApprovalError(RuntimeError):
    """Raised when an approval workflow cannot be completed safely."""
