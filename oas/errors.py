"""Exception types used across the framework."""
from __future__ import annotations


class OASError(Exception):
    """Base class for all framework errors."""


class ManifestError(OASError, ValueError):
    """A service manifest is structurally invalid."""


class ClaimError(OASError, ValueError):
    """A claim is malformed or violates provenance rules."""


class StoreError(OASError):
    """Storage-level misuse (for example nested transactions)."""


class PermissionDenied(OASError, PermissionError):
    """A service attempted an action its manifest does not permit."""


class MessageError(OASError):
    """A message could not be queued."""


class ServiceNotFound(OASError, LookupError):
    """No service with that id is registered."""


class ServiceDisabled(OASError):
    """The service is disabled (circuit breaker or explicit)."""


class ServiceNotBound(OASError):
    """No callable is bound to the service and its entrypoint cannot be loaded."""


class ServiceTimeout(OASError):
    """A service exceeded its configured timeout."""


class ServiceCancelled(OASError):
    """A cancelled (timed-out) run tried to keep working."""
