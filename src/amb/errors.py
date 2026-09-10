from __future__ import annotations


class AMBError(Exception):
    """Base class for board errors."""

    status_code = 400


class CharterViolation(AMBError):
    """A hard constitutional stop. Never overridable by quorum or payment."""

    status_code = 403


class PolicyViolation(AMBError):
    status_code = 403


class IllegalTransition(AMBError):
    status_code = 409


class ScopeDenied(AMBError):
    status_code = 403


class RateLimited(AMBError):
    status_code = 429


class NotFound(AMBError):
    status_code = 404


class SignatureInvalid(AMBError):
    status_code = 401
