"""Outcome taxonomy.

The whole point of this module is that runtime problems are *not* collapsed into
one generic exception. Every condition the engine can encounter has a stable
code, a category, and a declared disposition.
"""

from __future__ import annotations

from enum import Enum


class OutcomeCategory(str, Enum):
    """Top level classification returned to the caller."""

    SUCCESS = "SUCCESS"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"  # ran fine, the app said no
    RECOVERABLE = "RECOVERABLE"  # may succeed on retry or after intervention
    HARD_FAILURE = "HARD_FAILURE"  # do not retry blindly


class ErrorCode(str, Enum):
    NONE = "NONE"

    # business outcomes
    RECORD_NOT_FOUND = "RECORD_NOT_FOUND"
    ALREADY_PROCESSED = "ALREADY_PROCESSED"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    ACCOUNT_ON_HOLD = "ACCOUNT_ON_HOLD"

    # hard failures
    INVALID_INPUT = "INVALID_INPUT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    APPLICATION_ERROR = "APPLICATION_ERROR"
    CHECKPOINT_MISMATCH = "CHECKPOINT_MISMATCH"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    SURFACE_INCOMPATIBLE = "SURFACE_INCOMPATIBLE"
    ABORTED_BY_OPERATOR = "ABORTED_BY_OPERATOR"

    # recoverable
    MISSING_CONTROL = "MISSING_CONTROL"
    AMBIGUOUS_CONTROL = "AMBIGUOUS_CONTROL"
    UNEXPECTED_DIALOG = "UNEXPECTED_DIALOG"
    SLOW_LOAD = "SLOW_LOAD"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    SESSION_EXPIRED = "SESSION_EXPIRED"


#: Default category for each code. A capability may narrow this per outcome but
#: never widens SUCCESS onto a failure code.
DEFAULT_CATEGORY: dict[ErrorCode, OutcomeCategory] = {
    ErrorCode.NONE: OutcomeCategory.SUCCESS,
    ErrorCode.RECORD_NOT_FOUND: OutcomeCategory.BUSINESS_OUTCOME,
    ErrorCode.ALREADY_PROCESSED: OutcomeCategory.BUSINESS_OUTCOME,
    ErrorCode.INSUFFICIENT_FUNDS: OutcomeCategory.BUSINESS_OUTCOME,
    ErrorCode.ACCOUNT_ON_HOLD: OutcomeCategory.BUSINESS_OUTCOME,
    ErrorCode.INVALID_INPUT: OutcomeCategory.HARD_FAILURE,
    ErrorCode.VALIDATION_ERROR: OutcomeCategory.HARD_FAILURE,
    ErrorCode.PERMISSION_DENIED: OutcomeCategory.HARD_FAILURE,
    ErrorCode.APPLICATION_ERROR: OutcomeCategory.HARD_FAILURE,
    ErrorCode.CHECKPOINT_MISMATCH: OutcomeCategory.HARD_FAILURE,
    ErrorCode.POLICY_VIOLATION: OutcomeCategory.HARD_FAILURE,
    ErrorCode.SURFACE_INCOMPATIBLE: OutcomeCategory.HARD_FAILURE,
    ErrorCode.ABORTED_BY_OPERATOR: OutcomeCategory.HARD_FAILURE,
    ErrorCode.MISSING_CONTROL: OutcomeCategory.RECOVERABLE,
    ErrorCode.AMBIGUOUS_CONTROL: OutcomeCategory.RECOVERABLE,
    ErrorCode.UNEXPECTED_DIALOG: OutcomeCategory.RECOVERABLE,
    ErrorCode.SLOW_LOAD: OutcomeCategory.RECOVERABLE,
    ErrorCode.TRANSIENT_FAILURE: OutcomeCategory.RECOVERABLE,
    ErrorCode.SESSION_EXPIRED: OutcomeCategory.RECOVERABLE,
}

#: Codes where an automatic bounded retry is meaningful.
RETRYABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.SLOW_LOAD,
        ErrorCode.TRANSIENT_FAILURE,
        ErrorCode.SESSION_EXPIRED,
        ErrorCode.MISSING_CONTROL,
        ErrorCode.UNEXPECTED_DIALOG,
    }
)

#: Codes that should hand the session to a human rather than fail outright,
#: when a handoff channel is configured.
ESCALATABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.MISSING_CONTROL,
        ErrorCode.AMBIGUOUS_CONTROL,
        ErrorCode.UNEXPECTED_DIALOG,
        ErrorCode.PERMISSION_DENIED,
        ErrorCode.SESSION_EXPIRED,
    }
)


class SableauError(Exception):
    """Base for engine level failures that carry a taxonomy code."""

    code: ErrorCode = ErrorCode.TRANSIENT_FAILURE

    def __init__(self, message: str, code: ErrorCode | None = None, step_id: str | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.step_id = step_id

    @property
    def category(self) -> OutcomeCategory:
        return DEFAULT_CATEGORY[self.code]

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE


class PolicyViolation(SableauError):
    code = ErrorCode.POLICY_VIOLATION


class ControlResolutionError(SableauError):
    code = ErrorCode.MISSING_CONTROL


class AmbiguousControlError(SableauError):
    code = ErrorCode.AMBIGUOUS_CONTROL


class CheckpointFailed(SableauError):
    code = ErrorCode.CHECKPOINT_MISMATCH


class InvalidInput(SableauError):
    code = ErrorCode.INVALID_INPUT


class SurfaceIncompatible(SableauError):
    code = ErrorCode.SURFACE_INCOMPATIBLE


class TransientFailure(SableauError):
    code = ErrorCode.TRANSIENT_FAILURE


class OperatorAbort(SableauError):
    code = ErrorCode.ABORTED_BY_OPERATOR
