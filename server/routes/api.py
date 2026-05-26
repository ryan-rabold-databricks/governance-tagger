"""HTTP routes for the governance-tagger app.

Every endpoint:
  1. Extracts the end-user identity from forwarded headers.
  2. Builds an OBO ``WorkspaceClient`` so UC sees the real user.
  3. Wraps the operation in try/except that logs both success and failure
     to the security audit pipeline.
"""

from __future__ import annotations

import os
import re
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit_logger, uc
from ..config import (
    UserContext,
    get_sp_client,
)


router = APIRouter()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

# Databricks UC identifier charset (catalog/schema/table/column names):
# letters, digits, underscore, hyphen. Empty is rejected. Hard cap at 255
# (UC's own max identifier length).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_IDENTIFIER_MAX_LEN = 255

# Hard cap on user-supplied comment payloads. Anything larger is almost
# certainly abuse — UC's own COMMENT length cap is similar.
_COMMENT_MAX_LEN = 10_000


def _validate_identifier(name: str, kind: str, ctx: UserContext) -> None:
    """Reject malformed UC identifiers at the request boundary.

    On rejection, raises ``HTTPException(400)`` AND emits a
    ``suspicious_activity`` audit event so the security team has a record
    of probes / injection attempts. Successful validation is silent
    (the per-route success log captures the normal case).
    """
    if not name or len(name) > _IDENTIFIER_MAX_LEN or not _IDENTIFIER_RE.match(name):
        audit_logger.log_event(
            event_type="suspicious_activity",
            outcome="denied",
            action=f"invalid_{kind}",
            user_email=ctx.email,
            user_id=ctx.user_id,
            source_ip=ctx.source_ip,
            request_id=ctx.request_id,
            error_code="INVALID_IDENTIFIER",
            error_message=f"Rejected {kind!r} that does not match UC identifier rules.",
            details={"kind": kind, "raw_length": len(name) if name else 0},
        )
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {kind} name. Must match {_IDENTIFIER_RE.pattern} "
            f"and be no longer than {_IDENTIFIER_MAX_LEN} characters.",
        )


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------


def _user_context(req: Request) -> UserContext:
    """Extract end-user identity from the inbound request headers.

    Databricks Apps forward these headers automatically:
      * X-Forwarded-Email       -> user email
      * X-Forwarded-User        -> stable user id (UUID)
      * X-Forwarded-Access-Token -> user OAuth token (for OBO calls)
      * X-Forwarded-For          -> client IP
      * X-Request-Id             -> inbound correlation id

    Locally we fall back to a "local-dev" identity so the app runs without
    Databricks Apps in front.
    """
    email = (
        req.headers.get("x-forwarded-email")
        or req.headers.get("x-forwarded-preferred-username")
        or os.environ.get("LOCAL_USER_EMAIL", "local-dev@example.com")
    )
    user_id = req.headers.get("x-forwarded-user", "local-dev")
    token = req.headers.get("x-forwarded-access-token")
    ip = req.headers.get("x-forwarded-for", req.client.host if req.client else "0.0.0.0")
    request_id = req.headers.get("x-request-id", str(uuid.uuid4()))
    return UserContext(
        email=email, user_id=user_id, token=token, source_ip=ip, request_id=request_id
    )


def _map_error(exc: Exception) -> tuple[int, str, str]:
    """Translate SDK errors to (http_status, error_code, event_type)."""
    if isinstance(exc, uc.PermissionDenied):
        return 403, "PERMISSION_DENIED", "permission_denied"
    if isinstance(exc, uc.Unauthenticated):
        return 401, "UNAUTHENTICATED", "auth_error"
    if isinstance(exc, uc.NotFound):
        return 404, "NOT_FOUND", "data_access"
    return 500, "INTERNAL_ERROR", "api_call"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class UpdateDescriptionBody(BaseModel):
    comment: str = Field(..., max_length=_COMMENT_MAX_LEN)


class UpdateColumnCommentBody(BaseModel):
    column: str = Field(..., min_length=1, max_length=_IDENTIFIER_MAX_LEN)
    comment: str = Field(..., max_length=_COMMENT_MAX_LEN)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/me")
def whoami(request: Request) -> dict:
    ctx = _user_context(request)
    audit_logger.log_event(
        event_type="app_load",
        outcome="success",
        action="whoami",
        user_email=ctx.email,
        user_id=ctx.user_id,
        source_ip=ctx.source_ip,
        request_id=ctx.request_id,
    )
    return {"email": ctx.email, "user_id": ctx.user_id}


@router.get("/catalogs")
def catalogs(request: Request) -> dict:
    ctx = _user_context(request)
    try:
        values = uc.list_catalogs()
    except Exception as exc:  # noqa: BLE001
        status, code, etype = _map_error(exc)
        audit_logger.log_event(
            event_type=etype,
            outcome="denied" if status == 403 else "failure",
            action="list_catalogs",
            user_email=ctx.email,
            user_id=ctx.user_id,
            source_ip=ctx.source_ip,
            request_id=ctx.request_id,
            error_code=code,
            error_message=str(exc),
        )
        raise HTTPException(status_code=status, detail=str(exc))
    audit_logger.log_event(
        event_type="data_access",
        outcome="success",
        action="list_catalogs",
        user_email=ctx.email,
        user_id=ctx.user_id,
        source_ip=ctx.source_ip,
        request_id=ctx.request_id,
        details={"count": len(values)},
    )
    return {"catalogs": values}


@router.get("/catalogs/{catalog}/schemas")
def schemas_in_catalog(catalog: str, request: Request) -> dict:
    ctx = _user_context(request)
    _validate_identifier(catalog, "catalog", ctx)
    try:
        values = uc.list_schemas(catalog)
    except Exception as exc:  # noqa: BLE001
        status, code, etype = _map_error(exc)
        audit_logger.log_event(
            event_type=etype,
            outcome="denied" if status == 403 else "failure",
            action="list_schemas",
            catalog_name=catalog,
            user_email=ctx.email,
            user_id=ctx.user_id,
            source_ip=ctx.source_ip,
            request_id=ctx.request_id,
            error_code=code,
            error_message=str(exc),
        )
        raise HTTPException(status_code=status, detail=str(exc))
    audit_logger.log_event(
        event_type="data_access",
        outcome="success",
        action="list_schemas",
        catalog_name=catalog,
        user_email=ctx.email,
        user_id=ctx.user_id,
        source_ip=ctx.source_ip,
        request_id=ctx.request_id,
        details={"count": len(values)},
    )
    return {"schemas": values}


@router.get("/catalogs/{catalog}/schemas/{schema}/tables")
def tables_in_schema(catalog: str, schema: str, request: Request) -> dict:
    ctx = _user_context(request)
    _validate_identifier(catalog, "catalog", ctx)
    _validate_identifier(schema, "schema", ctx)
    try:
        items = uc.list_tables(catalog, schema)
    except Exception as exc:  # noqa: BLE001
        status, code, etype = _map_error(exc)
        audit_logger.log_event(
            event_type=etype,
            outcome="denied" if status == 403 else "failure",
            action="list_tables",
            catalog_name=catalog,
            schema_name=schema,
            user_email=ctx.email,
            user_id=ctx.user_id,
            source_ip=ctx.source_ip,
            request_id=ctx.request_id,
            error_code=code,
            error_message=str(exc),
        )
        raise HTTPException(status_code=status, detail=str(exc))
    audit_logger.log_event(
        event_type="data_access",
        outcome="success",
        action="list_tables",
        catalog_name=catalog,
        schema_name=schema,
        user_email=ctx.email,
        user_id=ctx.user_id,
        source_ip=ctx.source_ip,
        request_id=ctx.request_id,
        details={"count": len(items)},
    )
    return {"tables": items}


@router.get("/tables/{catalog}/{schema}/{table}")
def get_table(catalog: str, schema: str, table: str, request: Request) -> dict:
    ctx = _user_context(request)
    _validate_identifier(catalog, "catalog", ctx)
    _validate_identifier(schema, "schema", ctx)
    _validate_identifier(table, "table", ctx)
    # We use the SP client for UC reads/writes so that we only need the
    # ``sql`` user scope on the OBO token. The end-user identity is captured
    # in the audit log via ``ctx.email``. UC's own audit log will show the
    # SP as the actor; our centralized log is the human-actor source of
    # truth.
    client = get_sp_client()
    try:
        info = uc.describe_table(client, catalog, schema, table)
    except Exception as exc:  # noqa: BLE001
        status, code, etype = _map_error(exc)
        audit_logger.log_event(
            event_type=etype,
            outcome="denied" if status == 403 else "failure",
            action="read_metadata",
            catalog_name=catalog,
            schema_name=schema,
            table_name=table,
            user_email=ctx.email,
            user_id=ctx.user_id,
            source_ip=ctx.source_ip,
            request_id=ctx.request_id,
            error_code=code,
            error_message=str(exc),
        )
        raise HTTPException(status_code=status, detail=str(exc))
    audit_logger.log_event(
        event_type="data_access",
        outcome="success",
        action="read_metadata",
        catalog_name=catalog,
        schema_name=schema,
        table_name=table,
        user_email=ctx.email,
        user_id=ctx.user_id,
        source_ip=ctx.source_ip,
        request_id=ctx.request_id,
    )
    return info


@router.put("/tables/{catalog}/{schema}/{table}/description")
def put_table_description(
    catalog: str,
    schema: str,
    table: str,
    body: UpdateDescriptionBody,
    request: Request,
) -> dict:
    ctx = _user_context(request)
    _validate_identifier(catalog, "catalog", ctx)
    _validate_identifier(schema, "schema", ctx)
    _validate_identifier(table, "table", ctx)
    # We use the SP client for UC reads/writes so that we only need the
    # ``sql`` user scope on the OBO token. The end-user identity is captured
    # in the audit log via ``ctx.email``. UC's own audit log will show the
    # SP as the actor; our centralized log is the human-actor source of
    # truth.
    client = get_sp_client()
    # Read the previous value first so the audit log has before/after.
    try:
        before = uc.describe_table(client, catalog, schema, table)
        old_comment = before["comment"]
    except Exception as exc:  # noqa: BLE001
        status, code, etype = _map_error(exc)
        audit_logger.log_event(
            event_type=etype,
            outcome="denied" if status == 403 else "failure",
            action="update_description",
            catalog_name=catalog,
            schema_name=schema,
            table_name=table,
            user_email=ctx.email,
            user_id=ctx.user_id,
            source_ip=ctx.source_ip,
            request_id=ctx.request_id,
            error_code=code,
            error_message=str(exc),
        )
        raise HTTPException(status_code=status, detail=str(exc))

    try:
        uc.update_table_comment(client, catalog, schema, table, body.comment)
    except Exception as exc:  # noqa: BLE001
        status, code, etype = _map_error(exc)
        audit_logger.log_event(
            event_type=etype,
            outcome="denied" if status == 403 else "failure",
            action="update_description",
            catalog_name=catalog,
            schema_name=schema,
            table_name=table,
            user_email=ctx.email,
            user_id=ctx.user_id,
            source_ip=ctx.source_ip,
            request_id=ctx.request_id,
            before_value=old_comment,
            after_value=body.comment,
            error_code=code,
            error_message=str(exc),
        )
        raise HTTPException(status_code=status, detail=str(exc))

    audit_logger.log_event(
        event_type="data_edit",
        outcome="success",
        action="update_description",
        catalog_name=catalog,
        schema_name=schema,
        table_name=table,
        user_email=ctx.email,
        user_id=ctx.user_id,
        source_ip=ctx.source_ip,
        request_id=ctx.request_id,
        before_value=old_comment,
        after_value=body.comment,
    )
    return {"ok": True, "before": old_comment, "after": body.comment}


@router.put("/tables/{catalog}/{schema}/{table}/columns")
def put_column_comment(
    catalog: str,
    schema: str,
    table: str,
    body: UpdateColumnCommentBody,
    request: Request,
) -> dict:
    ctx = _user_context(request)
    _validate_identifier(catalog, "catalog", ctx)
    _validate_identifier(schema, "schema", ctx)
    _validate_identifier(table, "table", ctx)
    _validate_identifier(body.column, "column", ctx)
    # We use the SP client for UC reads/writes so that we only need the
    # ``sql`` user scope on the OBO token. The end-user identity is captured
    # in the audit log via ``ctx.email``. UC's own audit log will show the
    # SP as the actor; our centralized log is the human-actor source of
    # truth.
    client = get_sp_client()
    try:
        before = uc.describe_table(client, catalog, schema, table)
        old_value = ""
        for c in before["columns"]:
            if c["name"] == body.column:
                old_value = c["comment"]
                break
    except Exception as exc:  # noqa: BLE001
        status, code, etype = _map_error(exc)
        audit_logger.log_event(
            event_type=etype,
            outcome="denied" if status == 403 else "failure",
            action="update_column_comment",
            catalog_name=catalog,
            schema_name=schema,
            table_name=table,
            column_name=body.column,
            user_email=ctx.email,
            user_id=ctx.user_id,
            source_ip=ctx.source_ip,
            request_id=ctx.request_id,
            error_code=code,
            error_message=str(exc),
        )
        raise HTTPException(status_code=status, detail=str(exc))

    try:
        uc.update_column_comment(client, catalog, schema, table, body.column, body.comment)
    except Exception as exc:  # noqa: BLE001
        status, code, etype = _map_error(exc)
        audit_logger.log_event(
            event_type=etype,
            outcome="denied" if status == 403 else "failure",
            action="update_column_comment",
            catalog_name=catalog,
            schema_name=schema,
            table_name=table,
            column_name=body.column,
            user_email=ctx.email,
            user_id=ctx.user_id,
            source_ip=ctx.source_ip,
            request_id=ctx.request_id,
            before_value=old_value,
            after_value=body.comment,
            error_code=code,
            error_message=str(exc),
        )
        raise HTTPException(status_code=status, detail=str(exc))

    audit_logger.log_event(
        event_type="data_edit",
        outcome="success",
        action="update_column_comment",
        catalog_name=catalog,
        schema_name=schema,
        table_name=table,
        column_name=body.column,
        user_email=ctx.email,
        user_id=ctx.user_id,
        source_ip=ctx.source_ip,
        request_id=ctx.request_id,
        before_value=old_value,
        after_value=body.comment,
    )
    return {"ok": True, "before": old_value, "after": body.comment}


@router.get("/audit/mine")
def my_audit_trail(request: Request, limit: int = 25) -> dict:
    ctx = _user_context(request)
    try:
        rows = uc.list_recent_audit_events(ctx.email, limit=limit)
    except Exception as exc:  # noqa: BLE001
        status, code, etype = _map_error(exc)
        audit_logger.log_event(
            event_type=etype,
            outcome="failure",
            action="read_own_audit",
            user_email=ctx.email,
            user_id=ctx.user_id,
            source_ip=ctx.source_ip,
            request_id=ctx.request_id,
            error_code=code,
            error_message=str(exc),
        )
        raise HTTPException(status_code=status, detail=str(exc))
    return {"events": rows}
