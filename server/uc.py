"""Thin Unity Catalog access helpers.

All UC reads/writes for the app go through here. The functions are written
so that they accept either:

  * an SP-authenticated WorkspaceClient (for the central audit table); or
  * an OBO user-authenticated WorkspaceClient (for metadata edits, so the
    real user shows up in UC's own audit log).
"""

from __future__ import annotations

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import (
    NotFound,
    PermissionDenied,
    Unauthenticated,
)
from databricks import sql as dbsql

from .config import (
    AUDIT_FQN,
    WAREHOUSE_ID,
    get_workspace_host,
    get_sp_client,
)

# Hard ceiling on rows returned from the audit table read endpoint.
# Clamps any caller-supplied limit so the warehouse never sees a runaway query.
_AUDIT_QUERY_MAX_LIMIT = 200


__all__ = [
    "PermissionDenied",
    "Unauthenticated",
    "NotFound",
    "list_catalogs",
    "list_schemas",
    "list_tables",
    "describe_table",
    "update_table_comment",
    "update_column_comment",
]


def _sql_connection():
    """Return a SQL warehouse connection using the SP credentials.

    Only used by ``list_recent_audit_events``; everything else goes through
    the SDK so UC enforces permissions uniformly.
    """
    host = get_workspace_host().replace("https://", "").replace("http://", "")
    http_path = f"/sql/1.0/warehouses/{WAREHOUSE_ID}"
    cfg = get_sp_client().config

    def _credential_provider():
        def _inner():
            return cfg.authenticate()
        return _inner

    return dbsql.connect(
        server_hostname=host,
        http_path=http_path,
        credentials_provider=_credential_provider,
    )


def list_catalogs() -> list[str]:
    """All catalogs the app SP can see, sorted alphabetically.

    Uses the SDK's ``catalogs.list`` so UC permissions are enforced uniformly.
    """
    client = get_sp_client()
    return sorted(c.name for c in client.catalogs.list() if c.name)


def list_schemas(catalog: str) -> list[str]:
    """All user schemas in ``catalog``, sorted.

    Filters out ``information_schema`` and any double-underscore-prefixed
    schemas (e.g. ``__databricks_internal``) that aren't user-editable.
    """
    client = get_sp_client()
    return sorted(
        s.name
        for s in client.schemas.list(catalog_name=catalog)
        if s.name
        and s.name != "information_schema"
        and not s.name.startswith("__")
    )


def list_tables(catalog: str, schema: str) -> list[dict]:
    """All tables/views in ``catalog.schema``.

    Returns a list of ``{catalog, schema, table, comment}`` dicts, sorted by
    table name.
    """
    client = get_sp_client()
    items = [
        {
            "catalog": catalog,
            "schema": schema,
            "table": t.name,
            "comment": t.comment or "",
        }
        for t in client.tables.list(catalog_name=catalog, schema_name=schema)
        if t.name
    ]
    items.sort(key=lambda x: x["table"])
    return items


def describe_table(client: WorkspaceClient, catalog: str, schema: str, table: str) -> dict:
    """Return current description + columns + their comments for one table.

    We use the SDK's ``tables.get`` so that UC enforces the caller's perms.
    Passing the OBO-auth client here means UC's audit log records the human
    user, not the SP.
    """
    full = f"{catalog}.{schema}.{table}"
    info = client.tables.get(full_name=full)
    columns = []
    for col in info.columns or []:
        columns.append(
            {
                "name": col.name,
                "type": col.type_text,
                "comment": col.comment or "",
                "position": col.position,
            }
        )
    return {
        "catalog": catalog,
        "schema": schema,
        "table": table,
        "comment": info.comment or "",
        "owner": info.owner,
        "columns": columns,
    }


def update_table_comment(
    client: WorkspaceClient,
    catalog: str,
    schema: str,
    table: str,
    new_comment: str,
) -> None:
    """Set a new table-level description via COMMENT ON TABLE.

    SQL is more stable than the SDK ``tables.update`` API surface across
    versions, and it works uniformly for tables and views.
    """
    host = get_workspace_host().replace("https://", "").replace("http://", "")
    http_path = f"/sql/1.0/warehouses/{WAREHOUSE_ID}"
    cfg = client.config

    def _credential_provider():
        def _inner():
            return cfg.authenticate()
        return _inner

    safe_comment = new_comment.replace("'", "''")
    safe_catalog = catalog.replace("`", "")
    safe_schema = schema.replace("`", "")
    safe_table = table.replace("`", "")
    sql_query = (
        f"COMMENT ON TABLE `{safe_catalog}`.`{safe_schema}`.`{safe_table}` "
        f"IS '{safe_comment}'"
    )
    with dbsql.connect(
        server_hostname=host,
        http_path=http_path,
        credentials_provider=_credential_provider,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_query)


def update_column_comment(
    client: WorkspaceClient,
    catalog: str,
    schema: str,
    table: str,
    column: str,
    new_comment: str,
) -> None:
    """Set a new column-level comment via ALTER TABLE.

    The SDK's column-update primitives are limited, so we issue ALTER TABLE
    through the SQL warehouse using the caller's token so UC sees the real
    user.
    """
    host = get_workspace_host().replace("https://", "").replace("http://", "")
    http_path = f"/sql/1.0/warehouses/{WAREHOUSE_ID}"
    cfg = client.config

    def _credential_provider():
        def _inner():
            return cfg.authenticate()
        return _inner

    # ALTER TABLE ... ALTER COLUMN ... COMMENT '...'  — parameters can't be
    # bound for identifiers and COMMENT is a clause, not an expression,
    # so we have to inject. Escape single quotes defensively.
    safe_comment = new_comment.replace("'", "''")
    safe_catalog = catalog.replace("`", "")
    safe_schema = schema.replace("`", "")
    safe_table = table.replace("`", "")
    safe_column = column.replace("`", "")
    sql_query = (
        f"ALTER TABLE `{safe_catalog}`.`{safe_schema}`.`{safe_table}` "
        f"ALTER COLUMN `{safe_column}` COMMENT '{safe_comment}'"
    )
    with dbsql.connect(
        server_hostname=host,
        http_path=http_path,
        credentials_provider=_credential_provider,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_query)


def list_recent_audit_events(user_email: str, limit: int = 25) -> list[dict]:
    """Read the audit table for the requesting user's recent activity.

    ``limit`` is clamped to ``[1, _AUDIT_QUERY_MAX_LIMIT]`` so a malicious
    or buggy caller cannot trigger a runaway query. The audit table FQN is
    parameterized via ``AUDIT_CATALOG``/``AUDIT_SCHEMA``/``AUDIT_TABLE``
    env vars (see ``config.AUDIT_FQN``).
    """
    safe_limit = max(1, min(int(limit), _AUDIT_QUERY_MAX_LIMIT))
    sql_query = f"""
        SELECT event_timestamp, event_type, outcome, action,
               catalog_name, schema_name, table_name, column_name,
               before_value, after_value, error_message
        FROM {AUDIT_FQN}
        WHERE user_email = ?
        ORDER BY event_timestamp DESC
        LIMIT {safe_limit}
    """
    with _sql_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_query, (user_email,))
            rows = cur.fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "timestamp": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]),
                "event_type": r[1],
                "outcome": r[2],
                "action": r[3],
                "catalog": r[4],
                "schema": r[5],
                "table": r[6],
                "column": r[7],
                "before": r[8],
                "after": r[9],
                "error": r[10],
            }
        )
    return out
