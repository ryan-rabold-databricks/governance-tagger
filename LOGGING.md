# LOGGING — Reusable security-logging design pattern




This document describes a centralized, app-agnostic security-logging
pattern for Databricks Apps. The implementation lives in
`server/audit_logger/`. Drop that file into any other Databricks App,
set `APP_NAME`, grant the SP write on the audit table, and you have the
same telemetry pipeline with zero additional work.

## Why two tiers

| Tier | Destination | Latency | Lifetime | Queryable | Use case |
|---|---|---|---|---|---|
| 1 | stdout / stderr → Apps log stream (`/logz`) | ms | rolling buffer in the App pod | grep-only | live debugging, on-call |
| 2 | `app_audit.app_security_logs.events` (Delta) | seconds (batched) | indefinite, governed by UC retention | SQL, Genie, dashboards, alerts | forensics, compliance, dashboards |

Both tiers are fed by **the same `log_event()` call**. They cannot drift,
and a developer never has to choose between them.

> stdout = "what just happened?" — operations.
> UC table = "what happened over the last 90 days?" — security/audit.

## Event schema

Every event — regardless of app — has this shape. Optional fields are
nullable.

| Field | Type | Notes |
|---|---|---|
| `event_id` | STRING | UUID generated at emit time |
| `event_timestamp` | TIMESTAMP | UTC, ISO-8601 |
| `event_date` | DATE | Partition column, derived from `event_timestamp` |
| `app_name` | STRING | From the `APP_NAME` env var. Same table can serve many apps. |
| `event_type` | STRING | `app_load` / `api_call` / `data_access` / `data_edit` / `permission_denied` / `auth_error` / `suspicious_activity` |
| `outcome` | STRING | `success` / `failure` / `denied` / `allowed` |
| `user_email` | STRING | From `X-Forwarded-Email` |
| `user_id` | STRING | From `X-Forwarded-User` |
| `source_ip` | STRING | From `X-Forwarded-For` (best effort) |
| `request_id` | STRING | From `X-Request-Id`; correlates with the platform's request id |
| `catalog_name` / `schema_name` / `table_name` / `column_name` | STRING | UC object touched, when applicable |
| `action` | STRING | App-specific verb (`list_catalogs`, `list_schemas`, `list_tables`, `update_description`, ...) |
| `before_value` / `after_value` | STRING | For edits — the before/after of the changed field |
| `error_code` / `error_message` | STRING | When `outcome` is `failure` or `denied` |
| `details` | STRING (JSON) | Free-form extension; anything else worth keeping |

DDL is in this repository's create scripts; the column comments in UC
match this table.

## Two-tier flow

```mermaid
flowchart TD
    A[Route handler] -->|log_event(...)| L[SecurityEvent dataclass]
    L --> J[to_json_line]
    J -->|outcome in success/allowed| OUT[stdout]
    J -->|outcome in failure/denied<br/>OR event_type in permission_denied/auth_error/suspicious_activity| ERR[stderr]
    L --> Q[bounded queue<br/>max 1000 events]
    Q --> W[background writer thread<br/>batches 50 events / 2s]
    W -->|INSERT via SQL warehouse| T[(app_audit.app_security_logs.events)]
    Q -.->|on overflow| D[drop event,<br/>emit drop notice to stderr]
    OUT --> APP[Apps /logz endpoint]
    ERR --> APP
```

Key properties:

- **The request thread never blocks on UC writes.** `log_event()` does
  the stdout emit synchronously (cheap), pushes onto a bounded queue,
  and returns. A daemon thread drains the queue.
- **Overflow is loud, not silent.** When the queue is full we drop the
  event and emit a `queue_full_drop` notice to stderr so on-call sees
  the dropped event.
- **Failure is contained.** UC write failures (warehouse cold, network
  blip, permissions) are caught in the daemon and written to stderr.
  The user request still succeeds — they don't see a 500 because we
  couldn't insert a log row.

## Concrete examples

### App load (Tier 1 stdout line)

```json
{
  "event_id": "ee01bf72-fc1f-4593-b407-01aa57879e49",
  "event_timestamp": "2026-05-19T14:09:26.140294+00:00",
  "app_name": "governance-tagger",
  "event_type": "app_load",
  "outcome": "success",
  "user_email": "alice@example.com",
  "user_id": "73701527069100@7474647046080636",
  "source_ip": "134.238.164.15,107.20.57.58, 10.149.65.160",
  "request_id": "f76679bd-2a33-4d79-bb1b-481856a98dea",
  "action": "whoami"
}
```

### Data edit (success)

```json
{
  "event_type": "data_edit",
  "outcome": "success",
  "user_email": "alice@example.com",
  "catalog_name": "app_audit",
  "schema_name": "clinical",
  "table_name": "patient_enrollment",
  "action": "update_description",
  "before_value": "Patient enrollments across clinical trials. PHI handled per IRB protocol.",
  "after_value": "Clinical trial enrollment records — governed by clinical data steward. Updated via Governance Tagger."
}
```

### Permission denied (security-relevant — goes to stderr)

```json
{
  "event_type": "permission_denied",
  "outcome": "denied",
  "user_email": "alice@example.com",
  "catalog_name": "app_audit",
  "schema_name": "clinical",
  "table_name": "patient_enrollment",
  "action": "update_description",
  "error_code": "PERMISSION_DENIED",
  "error_message": "PERMISSION_DENIED: User does not have MODIFY on Table 'app_audit.clinical.patient_enrollment'."
}
```

### Suspicious activity (emitted by a downstream rule, not the app itself)

You don't have to log this from the app — instead, the alert system
(below) detects the pattern in the audit table. But if your app does
detect it (e.g., five rapid denials from one user), the schema supports
it directly:

```json
{
  "event_type": "suspicious_activity",
  "outcome": "denied",
  "user_email": "alice@example.com",
  "action": "rapid_denials",
  "details": "{\"window_minutes\": 5, \"denial_count\": 7}"
}
```

## Querying the audit table

### All permission denials in the last 24 hours

```sql
SELECT event_timestamp, app_name, user_email, catalog_name, schema_name,
       table_name, action, error_message
FROM app_audit.app_security_logs.events
WHERE event_type IN ('permission_denied', 'auth_error')
  AND event_timestamp > current_timestamp() - INTERVAL 24 HOURS
ORDER BY event_timestamp DESC;
```

### All edits by a specific user in the last 30 days

```sql
SELECT event_timestamp, app_name, catalog_name, schema_name, table_name,
       column_name, action, before_value, after_value
FROM app_audit.app_security_logs.events
WHERE user_email = 'alice@example.com'
  AND event_type = 'data_edit'
  AND event_timestamp > current_timestamp() - INTERVAL 30 DAYS
ORDER BY event_timestamp DESC;
```

### Suspicious patterns — users with 5+ denials in the same hour

```sql
WITH denials AS (
  SELECT user_email,
         date_trunc('HOUR', event_timestamp) AS hour,
         count(*) AS denial_count
  FROM app_audit.app_security_logs.events
  WHERE event_type = 'permission_denied'
    AND event_timestamp > current_timestamp() - INTERVAL 7 DAYS
  GROUP BY 1, 2
)
SELECT user_email, hour, denial_count
FROM denials
WHERE denial_count >= 5
ORDER BY hour DESC, denial_count DESC;
```

### Cross-app activity timeline for a specific UC object

```sql
SELECT event_timestamp, app_name, user_email, action, outcome,
       before_value, after_value
FROM app_audit.app_security_logs.events
WHERE catalog_name = 'app_audit'
  AND schema_name = 'clinical'
  AND table_name = 'patient_enrollment'
ORDER BY event_timestamp DESC
LIMIT 200;
```

## How to use this in another app

1. **Copy `server/audit_logger/`** into your new project.
2. **Set the env vars** in your app's `app.yaml`:
   ```yaml
   env:
     - name: APP_NAME
       value: my-other-app
     - name: AUDIT_CATALOG
       value: app_audit
     - name: AUDIT_SCHEMA
       value: app_security_logs
     - name: AUDIT_TABLE
       value: events
     - name: DATABRICKS_WAREHOUSE_ID
       value: "<warehouse-id>"
   ```
3. **Grant the new app's SP** write on the audit table:
   ```sql
   GRANT USE SCHEMA ON SCHEMA app_audit.app_security_logs TO `<new-sp>`;
   GRANT MODIFY, SELECT ON TABLE app_audit.app_security_logs.events TO `<new-sp>`;
   ```
4. **Start the writer at app boot** (FastAPI lifespan, Flask `before_first_request`,
   or any equivalent):
   ```python
   from server import security_log
   security_log.start()
   ```
5. **Call `log_event(...)` from every route handler** for both success
   and failure paths.

Because `app_name` is part of every row, multiple apps can share this
table without colliding. Filtered dashboards and alerts can be scoped
per app or rolled up across them.

## Alerting

Databricks SQL Alerts can be wired directly to this table. Example —
fire when any single user generates more than 10 permission denials in
the last hour across any app:

```sql
SELECT user_email, count(*) AS denials
FROM app_audit.app_security_logs.events
WHERE event_type = 'permission_denied'
  AND event_timestamp > current_timestamp() - INTERVAL 1 HOUR
GROUP BY user_email
HAVING count(*) > 10;
```

Configure the alert with:
- **Schedule**: every 5 minutes.
- **Condition**: row count > 0.
- **Notification**: Slack channel / PagerDuty / email distribution.

The same pattern works for: edits to tagged-sensitive tables,
non-business-hours activity, edits by a service principal you didn't
expect to write, etc. The schema is rich enough that you almost never
need to extend it for a new alert.

## Why this design will scale

- **One table per metastore, not per app.** Adding the eleventh app
  costs you one env var and one `GRANT` — not a new logging stack.
- **Stdout JSON is structured, not human-prose.** Anything in `/logz`
  can be re-parsed by a downstream collector (Fluent Bit, Vector,
  Datadog) and shipped to your existing SIEM if you have one.
- **Partitioning by `event_date`** keeps queries fast as the table
  grows; UC's predictive optimization handles compaction.
- **The schema is intentionally flat** (no map/array columns) so it
  plays well with Genie, dashboards, and downstream materialized
  views — including governance teams who want to self-serve.
