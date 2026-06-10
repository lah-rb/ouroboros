"""Shared exception types for flow execution.

These errors may be raised by the loader (during pre-compute, ref resolution,
or prompt rendering) or by the runtime (during step execution or tail-call
handling). They live here, independent of either module, so both sides can
raise and catch them without importing each other.

Module-local errors (PathTraversalError, PersistenceError, TemplateError, etc.)
stay with their owning modules — they describe conditions internal to a single
component. This file is reserved for errors that genuinely cross module
boundaries in the execution pipeline.
"""

from __future__ import annotations


class FlowRuntimeError(Exception):
    """Raised when the flow execution pipeline encounters an unrecoverable error.

    Used by both the loader (ref resolution, formatter failure, prompt
    rendering) and the runtime (step execution, resolver failure).
    """

    pass


class MaxStepsExceeded(FlowRuntimeError):
    """Raised when a flow exceeds the maximum step count (infinite loop guard)."""

    pass


class MissingContextError(FlowRuntimeError):
    """Raised when a step's required context keys are not present in the accumulator."""

    pass


class MissingInputError(FlowRuntimeError):
    """Raised when a flow's required inputs are not provided at invocation time."""

    pass
