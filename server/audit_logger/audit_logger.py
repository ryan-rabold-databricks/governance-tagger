"""Reusable security logging module for Databricks Apps.

DROP-IN MODULE — no app-specific imports. All configuration is read from
environment variables so every Databricks App can use this without
modification.

Two-tier design:

  Tier 1 — stdout/stderr structured JSON
    Every event is a single JSON line. Successes go to stdout; security-
    relevant failures go to stderr. Databricks Apps captures both and
    exposes them in the Apps UI (`/logz`). Ephemeral but immediate.

  Tier 2 — UC Delta table at <AUDIT_CATALOG>.<AUDIT_SCHEMA>.<AUDIT_TABLE>
    Every event is also enqueued for async write to a centralized UC Delta
    table via a SQL warehouse. Durable, queryable, governed. The same
    table serves every Databricks App in the workspace — each row is
    stamped with `app_name` so writers don't collide.

Both tiers are fed from the same `log_event` call so the two destinations
can never drift out of sync.

Environment variables
---------------------
    APP_NAME                  (required) Identifies the app in the audit table.
    DATABRICKS_WAREHOUSE_ID   (required) Set via app.yaml resources block.
    AUDIT_CATALOG             Default: app_audit
    AUDIT_SCHEMA              Default: app_security_logs
    AUDIT_TABLE               Default: events

SDK auth env vars (DATABRICKS_HOST, DATABRICKS_CLIENT_ID,
DATABRICKS_CLIENT_SECRET) are injected automatically by the Apps runtime.
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
from enum import Enum
from typing import Any, Optional

from databricks import sql as dbsql
from databricks.sdk import WorkspaceClient


# ---------------------------------------------------------------------------
# Config (env-var driven, no per-app imports)
# ---------------------------------------------------------------------------

APP_NAME = os.environ.get("APP_NAME", "unnamed-app")
AUDIT_CATALOG = os.environ.get("AUDIT_CATALOG", "app_audit")
AUDIT_SCHEMA = os.environ.get("AUDIT_SCHEMA", "app_security_logs")
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "events")
AUDIT_FQN = f"{AUDIT_CATALOG}.{AUDIT_SCHEMA}.{AUDIT_TABLE}"
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID")


def _workspace_host() -> str:
    host = os.environ.get("DATABRICKS_HOST", "")
    if host and not host.startswith("http"):
        host = f"https://{host}"
    if host:
        return host
    return WorkspaceClient().config.host


def _sp_client() -> WorkspaceClient:
    """WorkspaceClient using the app's service principal credentials.

    Inside an App the SDK auto-discovers DATABRICKS_CLIENT_ID / _SECRET.
    Locally it falls back to the developer's CLI profile.
    """
    if os.environ.get("DATABRICKS_CLIENT_ID"):
        return WorkspaceClient()
    profile = os.environ.get("DATABRICKS_PROFILE")
    return WorkspaceClient(profile=profile) if profile else WorkspaceClient()


# ---------------------------------------------------------------------------
# Event schema (matches the central table DDL)
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    """Canonical event types written to the audit table.

    Subclassing ``str`` means an ``EventType`` member is also a string —
    JSON serialization, the UC Delta INSERT, and set-membership checks
    against raw strings all keep working. Callers can still pass a raw
    string if they need to extend the taxonomy at runtime.
    """

    APP_LOAD = "app_load"
    API_CALL = "api_call"
    DATA_ACCESS = "data_access"
    DATA_EDIT = "data_edit"
    PERMISSION_DENIED = "permission_denied"
    AUTH_ERROR = "auth_error"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"


@dataclass
class SecurityEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    app_name: str = APP_NAME
    event_type: str = EventType.API_CALL
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

    def __post_init__(self) -> None:
        # databricks-sql-connector's parameter binder checks ``type(v) is str``
        # (not isinstance), so an ``EventType`` member — which subclasses str —
        # is rejected at INSERT time. Coerce any Enum to its raw string here so
        # the row built for Tier 2 always has a plain ``str``.
        if isinstance(self.event_type, Enum):
            self.event_type = self.event_type.value

    def to_json_line(self) -> str:
        d = asdict(self)
        if d.get("details") is not None:
            d["details"] = json.dumps(d["details"], default=str, sort_keys=True)
        return json.dumps(d, default=str)


# ---------------------------------------------------------------------------
# Tier 1: stdout/stderr JSON
# ---------------------------------------------------------------------------

_FAIL_OUTCOMES = {"denied", "failure", "error"}
_SECURITY_TYPES = {
    EventType.PERMISSION_DENIED,
    EventType.AUTH_ERROR,
    EventType.SUSPICIOUS_ACTIVITY,
}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("audit_logger")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    out = logging.StreamHandler(sys.stdout)
    out.setLevel(logging.INFO)
    out.setFormatter(_JsonFormatter())
    out.addFilter(lambda r: r.levelno < logging.ERROR)
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.ERROR)
    err.setFormatter(_JsonFormatter())
    logger.addHandler(out)
    logger.addHandler(err)
    logger.propagate = False
    return logger


_logger = _build_logger()


# ---------------------------------------------------------------------------
# Tier 2: async writer to UC Delta via SQL warehouse
# ---------------------------------------------------------------------------


class _AuditWriter:
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
            sys.stderr.write(
                json.dumps(
                    {
                        "audit_writer": "disabled",
                        "reason": "DATABRICKS_WAREHOUSE_ID not set",
                        "app_name": APP_NAME,
                    }
                )
                + "\n"
            )
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="audit-writer", daemon=True)
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
            sys.stderr.write(
                json.dumps(
                    {
                        "audit_writer": "queue_full_drop",
                        "event_id": event.event_id,
                        "app_name": APP_NAME,
                    }
                )
                + "\n"
            )

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
            except Exception as exc:
                sys.stderr.write(
                    json.dumps(
                        {
                            "audit_writer": "flush_failed",
                            "error": str(exc),
                            "batch_size": len(batch),
                            "app_name": APP_NAME,
                        }
                    )
                    + "\n"
                )

    def _flush(self, batch: list[SecurityEvent]) -> None:
        host = _workspace_host().replace("https://", "").replace("http://", "")
        http_path = f"/sql/1.0/warehouses/{WAREHOUSE_ID}"
        cfg = _sp_client().config

        def _credential_provider():
            def _inner():
                return cfg.authenticate()
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
                            e.event_timestamp[:10],
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
                    ["(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"]
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
    """Start the background audit writer. Call once at app startup."""
    _writer.start()


def stop() -> None:
    """Stop the background writer cleanly. Call once at app shutdown."""
    _writer.stop()


def log_event(**kwargs: Any) -> SecurityEvent:
    """Log a security event to both tiers (stdout/stderr + UC Delta)."""
    event = SecurityEvent(**kwargs)
    line = event.to_json_line()
    if event.outcome in _FAIL_OUTCOMES or event.event_type in _SECURITY_TYPES:
        _logger.error(line)
    else:
        _logger.info(line)
    _writer.submit(event)
    return event
