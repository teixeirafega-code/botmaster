from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="")
operation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("operation_id", default="")
execution_mode_var: contextvars.ContextVar[str] = contextvars.ContextVar("execution_mode", default="paper")


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def get_correlation_id() -> str:
    current = correlation_id_var.get()
    if current:
        return current
    generated = new_correlation_id()
    correlation_id_var.set(generated)
    return generated


def get_operation_id() -> str:
    current = operation_id_var.get()
    if current:
        return current
    generated = str(uuid.uuid4())
    operation_id_var.set(generated)
    return generated


@contextmanager
def flow_context(*, correlation_id: str | None = None, operation_id: str | None = None, execution_mode: str | None = None) -> Iterator[None]:
    corr_token = correlation_id_var.set(correlation_id or new_correlation_id())
    op_token = operation_id_var.set(operation_id or str(uuid.uuid4()))
    mode_token = execution_mode_var.set(execution_mode or execution_mode_var.get())
    try:
        yield
    finally:
        correlation_id_var.reset(corr_token)
        operation_id_var.reset(op_token)
        execution_mode_var.reset(mode_token)

