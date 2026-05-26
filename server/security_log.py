"""Reusable security logging module for Databricks Apps.

Two-tier design:

  Tier 1 — stdout/stderr structured JSON
    Every event is written as a single JSON line to stdout (info/success)
    or stderr (security-relevant failure). Databricks Apps captures these
    automatically and exposes them in the Apps UI (the ``/logz`` endpoint).
    This is ephemeral but immediate — perfect for live debugging.

  Tier 2 — UC Delta table at ``<catalog>.app_security_logs.events``
    Every event is also enqueued for async write to a centralized UC Delta
    table via the SQL warehouse resource. This is durable, queryable, and
    governed — exactly what auditors and security teams want.

Both tiers are fed from the same ``log_event`` call so the two destinations
can never drift out of sync.

Drop this module into any other Databricks App by:
  1. Copying ``security_log.py`` into your project.
  2. Setting the ``APP_NAME`` env var.
  3. Granting your service principal write access to the audit table.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from databricks import sql as dbsql

from .config import (
    APP_NAME,
    AUDIT_CATALOG,
    AUDIT_FQN,
    AUDIT_SCHEMA,
    AUDIT_TABLE,
    IS_DATABRICKS_APP,
    WAREHOUSE_ID,
    get_workspace_host,
    get_sp_client,
)


# ---------------------------------------------------------------------------
# Event schema
# ---------------------------------------------------------------------------


@dataclass
class SecurityEvent:
    """Canonical security event — same shape goes to stdout and to UC."""

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    app_name: str = APP_NAME
    event_type: str = "api_call"
    outcome: str = "success"
    user_email: Optional[str] = None
    user_id: Optional[str] = None
    source_ip: Optional[str] = None
    request_id: Optional[str] = None
    catalog_name: Optional[str] = None
    schema_name: Optional[str] = None
    table_name: Optional[str] = None
    column_name: Optional[str] = None
    action: Optional[str] = None
    before_value: Optional[str] = None
    after_value: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    details: Optional[dict] = None

    def to_json_line(self) -> str:
        d = asdict(self)
        # Details is a dict; JSON-encode it so it stays on one line and is
        # also storable as a STRING column in Delta.
        if d.get("details") is not None:
            d["details"] = json.dumps(d["details"], default=str, sort_keys=True)
        return json.dumps(d, default=str)


# ---------------------------------------------------------------------------
# Tier 1: stdout/stderr JSON logger
# ---------------------------------------------------------------------------


_FAIL_OUTCOMES = {"denied", "failure", "error"}
_SECURITY_TYPES = {"permission_denied", "auth_error", "suspicious_activity"}


class _JsonFormatter(logging.Formatter):
    """Pass-through formatter — message is already a JSON string."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        return record.getMessage()


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("governance.audit")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    out_handler = logging.StreamHandler(sys.stdout)
    out_handler.setLevel(logging.INFO)
    out_handler.setFormatter(_JsonFormatter())
    out_handler.addFilter(lambda r: r.levelno < logging.ERROR)
    err_handler = logging.StreamHandler(sys.stderr)
    err_handler.setLevel(logging.ERROR)
    err_handler.setFormatter(_JsonFormatter())
    logger.addHandler(out_handler)
    logger.addHandler(err_handler)
    logger.propagate = False
    return logger


_logger = _build_logger()


# ---------------------------------------------------------------------------
# Tier 2: async writer to UC Delta via SQL warehouse
# ---------------------------------------------------------------------------


class _AuditWriter:
    """Fire-and-forget UC writer with a bounded queue.

    Logging must NEVER block the request thread. We push events onto a
    bounded ``queue.Queue`` (drop-on-overflow with a stderr warning) and a
    background daemon thread flushes them to UC.
    """

    QUEUE_SIZE = 1000
    BATCH_MAX = 50
    BATCH_INTERVAL_SEC = 2.0

    def __init__(self) -> None:
        self._queue: queue.Queue[SecurityEvent] = queue.Queue(maxsize=self.QUEUE_SIZE)
        self._enabled = bool(WAREHOUSE_ID)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not self._enabled:
            _logger.warning(
                json.dumps(
                    {
                        "audit_writer": "disabled",
                        "reason": "DATABRICKS_WAREHOUSE_ID env var not set",
                    }
                )
            )
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="audit-writer", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def submit(self, event: SecurityEvent) -> None:
        if not self._enabled:
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # Don't block — drop and shout to stderr.
            sys.stderr.write(
                json.dumps(
                    {
                        "audit_writer": "queue_full_drop",
                        "event_id": event.event_id,
                    }
                )
                + "\n"
            )

    # -- background loop ---------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            batch: list[SecurityEvent] = []
            deadline = time.monotonic() + self.BATCH_INTERVAL_SEC
            while len(batch) < self.BATCH_MAX:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(self._queue.get(timeout=remaining))
                except queue.Empty:
                    break
            if not batch:
                continue
            try:
                self._flush(batch)
            except Exception as exc:  # noqa: BLE001
                # Logger to stderr — never re-raise out of the daemon.
                sys.stderr.write(
                    json.dumps(
                        {
                            "audit_writer": "flush_failed",
                            "error": str(exc),
                            "batch_size": len(batch),
                        }
                    )
                    + "\n"
                )

    def _flush(self, batch: list[SecurityEvent]) -> None:
        host = get_workspace_host().replace("https://", "").replace("http://", "")
        http_path = f"/sql/1.0/warehouses/{WAREHOUSE_ID}"
        # Re-use the SP auth from the SDK so we don't need to deal with PAT.
        cfg = get_sp_client().config

        def _credential_provider():
            def _inner():
                headers = cfg.authenticate()
                return headers  # already dict like {"Authorization": "Bearer ..."}
            return _inner

        with dbsql.connect(
            server_hostname=host,
            http_path=http_path,
            credentials_provider=_credential_provider,
        ) as conn:
            with conn.cursor() as cur:
                rows = []
                for e in batch:
                    rows.append(
                        (
                            e.event_id,
                            e.event_timestamp,
                            e.event_timestamp[:10],  # event_date partition
                            e.app_name,
                            e.event_type,
                            e.outcome,
                            e.user_email,
                            e.user_id,
                            e.source_ip,
                            e.request_id,
                            e.catalog_name,
                            e.schema_name,
                            e.table_name,
                            e.column_name,
                            e.action,
                            e.before_value,
                            e.after_value,
                            e.error_code,
                            e.error_message,
                            json.dumps(e.details, default=str)
                            if e.details is not None
                            else None,
                        )
                    )
                placeholders = ",".join(
                    [
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    ]
                    * len(rows)
                )
                flat = [v for row in rows for v in row]
                sql = (
                    f"INSERT INTO {AUDIT_FQN} "
                    "(event_id, event_timestamp, event_date, app_name, event_type, "
                    "outcome, user_email, user_id, source_ip, request_id, "
                    "catalog_name, schema_name, table_name, column_name, action, "
                    "before_value, after_value, error_code, error_message, details) "
                    f"VALUES {placeholders}"
                )
                cur.execute(sql, flat)


_writer = _AuditWriter()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start() -> None:
    """Start the background audit writer thread. Call once at app startup."""
    _writer.start()


def stop() -> None:
    """Stop the background writer cleanly at shutdown."""
    _writer.stop()


def log_event(**kwargs: Any) -> SecurityEvent:
    """Log a security event to both tiers.

    Example::

        log_event(
            event_type="data_edit",
            outcome="success",
            user_email=ctx.email,
            catalog_name="app_audit",
            schema_name="clinical",
            table_name="patient_enrollment",
            action="update_description",
            before_value=old,
            after_value=new,
        )
    """
    event = SecurityEvent(**kwargs)

    # Tier 1 — stdout / stderr.
    line = event.to_json_line()
    if event.outcome in _FAIL_OUTCOMES or event.event_type in _SECURITY_TYPES:
        _logger.error(line)
    else:
        _logger.info(line)

    # Tier 2 — UC Delta via background writer.
    _writer.submit(event)
    return event
