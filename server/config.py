"""Centralized configuration and auth helpers.

This module is the single place where the app figures out:
  * what environment it is running in (Databricks App vs local dev)
  * which Unity Catalog catalog / tag key / audit table to use
  * how to mint OAuth tokens for both the *service principal* (app identity)
    and the *end user* (on-behalf-of identity)

Two distinct identities are used by the app:

  1. The **service principal** identity. The app itself uses this to write
     to the central audit table. The Databricks runtime injects
     ``DATABRICKS_CLIENT_ID`` / ``DATABRICKS_CLIENT_SECRET`` env vars
     automatically inside an App.

  2. The **on-behalf-of user** identity. For UC metadata edits (description,
     column comments) we want UC's own audit log to record the *real* user,
     not the SP. Databricks Apps forwards the user's OAuth token in the
     ``X-Forwarded-Access-Token`` header on every inbound request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from databricks.sdk import WorkspaceClient


IS_DATABRICKS_APP: bool = bool(os.environ.get("DATABRICKS_APP_NAME"))

APP_NAME: str = os.environ.get("APP_NAME", "governance-tagger")

AUDIT_CATALOG: str = os.environ.get("AUDIT_CATALOG", "app_audit")
AUDIT_SCHEMA: str = os.environ.get("AUDIT_SCHEMA", "app_security_logs")
AUDIT_TABLE: str = os.environ.get("AUDIT_TABLE", "events")
AUDIT_FQN: str = f"{AUDIT_CATALOG}.{AUDIT_SCHEMA}.{AUDIT_TABLE}"

WAREHOUSE_ID: str | None = os.environ.get("DATABRICKS_WAREHOUSE_ID")


@dataclass
class UserContext:
    """Identity of the human user making a request, derived from headers.

    Populated from the FastAPI request via ``get_user_context``. We deliberately
    keep this small — anything that ends up in the audit log is captured here.
    """

    email: str
    user_id: str
    token: str | None
    source_ip: str
    request_id: str


def get_sp_client() -> WorkspaceClient:
    """WorkspaceClient authenticated as the app's service principal.

    Inside an App the SDK auto-discovers the injected client_id/secret.
    Locally it falls back to the developer's CLI profile.
    """
    if IS_DATABRICKS_APP:
        return WorkspaceClient()
    profile = os.environ.get("DATABRICKS_PROFILE", "<your_databricks_profile>")
    return WorkspaceClient(profile=profile)


def get_user_client(token: str | None) -> WorkspaceClient:
    """WorkspaceClient authenticated as the end user via their OBO token.

    Falls back to the SP client when no user token is available (e.g. local
    dev with no forwarded header) so the app remains testable.

    NOTE: inside a Databricks App the runtime auto-injects ``DATABRICKS_HOST``
    / ``DATABRICKS_CLIENT_ID`` / ``DATABRICKS_CLIENT_SECRET``. If we just
    pass ``host=`` and ``token=``, the SDK still detects the env-var OAuth
    credentials and raises "more than one authorization method configured".
    ``auth_type='pat'`` pins the client to bearer-token auth and ignores
    the SP env vars.
    """
    if token:
        host = get_workspace_host()
        return WorkspaceClient(host=host, token=token, auth_type="pat")
    return get_sp_client()


def get_workspace_host() -> str:
    """Workspace base URL, including the ``https://`` scheme.

    Inside an App ``DATABRICKS_HOST`` is just the hostname, so we add the
    scheme manually. Locally the SDK config already has it.
    """
    if IS_DATABRICKS_APP:
        host = os.environ.get("DATABRICKS_HOST", "")
        if host and not host.startswith("http"):
            host = f"https://{host}"
        return host
    return get_sp_client().config.host
