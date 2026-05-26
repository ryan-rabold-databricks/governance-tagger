# Governance Tagger

A [Databricks App](https://docs.databricks.com/en/dev-tools/databricks-apps/index.html)
for non-technical governance teams to curate Unity Catalog table and column
descriptions across a data estate — with a centralized, queryable security
audit log behind every action.

> **Status:** community template, Apache 2.0 licensed. Not an officially
> supported Databricks product — fork, modify, and run it however suits
> your governance model. The audit-logging pattern is the standalone
> [databricks-app-template](https://github.com/ryan-rabold-databricks/databricks-app-template)
> repo; this app shows it wired into a full-stack FastAPI + React app.

## What this app does

The governance team picks a **catalog** and a **schema** from two cascading
dropdowns. The app lists every Unity Catalog table in that schema. The user
clicks a table, sees its current description and column list, and edits
inline. Every change is recorded in the centralized audit table.

The UI is intentionally simple — no SQL, no jargon, no Unity Catalog
internals visible. The audience is a domain steward, not a data engineer.

## Who this app is for

- **Domain stewards** (clinical, finance, etc.) who own metadata quality
  for their data domain but should not need a SQL warehouse or notebook.
- **Governance leads** who need a single source of truth for "who edited
  what, when, and from where" across every internal app.
- **Security teams** who need to monitor for permission denials and
  suspicious patterns without parsing app stdout.

## Deploy

This repo is a [Databricks Asset Bundle](https://docs.databricks.com/dev-tools/bundles/index.html).
Two equivalent ways to deploy it — pick whichever fits your setup.

### Option A — From the Databricks workspace UI (no local tools required)

1. In your Databricks workspace, click **Workspace** in the left sidebar.
2. Click **+ Add → Git Folder**. Paste the repo URL:
   `https://github.com/ryan-rabold-databricks/governance-tagger`
3. Click **Create** — the workspace clones the repo.
4. Open the cloned folder and click the `databricks.yml` file. A **Bundle**
   panel appears with **Validate** and **Deploy** buttons. Click **Deploy**
   and select the `dev` target.
5. Once the deploy finishes, navigate to **Compute → Apps → governance-tagger**.
   You'll be prompted to attach a **SQL warehouse** (the `sql-warehouse`
   resource declared in `manifest.yaml`). Pick any warehouse the app SP
   has `CAN_USE` on.
6. Click **Start** on the app.

### Option B — From the CLI (Databricks CLI v0.239.0+)

```bash
# 1. Clone locally
git clone https://github.com/ryan-rabold-databricks/governance-tagger
cd governance-tagger

# 2. Edit databricks.yml: set workspace.host to your workspace URL,
#    or remove the line entirely if you've configured a default profile.

# 3. Validate + deploy
databricks bundle validate -t dev
databricks bundle deploy -t dev

# 4. Start the app (DAB doesn't auto-start Apps resources)
databricks bundle run governance_tagger -t dev
```

The built React SPA (`frontend/dist/`) is committed to the repo so neither
flow requires `npm`. If you edit the frontend source, rebuild before
deploying:

```bash
cd frontend && npm install && npm run build && cd ..
```

## Required resources & grants

The app needs a SQL warehouse resource (attached via the App UI or `apps
update` API) and these UC grants on the app's service principal:

```sql
-- Replace <SP_CLIENT_ID> with the value of service_principal_client_id from
-- `databricks apps get governance-tagger`
GRANT USE CATALOG, SELECT ON CATALOG app_audit TO `<SP_CLIENT_ID>`;
GRANT USE SCHEMA, MODIFY, SELECT ON SCHEMA app_audit.clinical TO `<SP_CLIENT_ID>`;
GRANT USE SCHEMA, MODIFY, SELECT ON SCHEMA app_audit.healthcare_demo TO `<SP_CLIENT_ID>`;
GRANT USE SCHEMA ON SCHEMA app_audit.app_security_logs TO `<SP_CLIENT_ID>`;
GRANT MODIFY, SELECT ON TABLE app_audit.app_security_logs.events TO `<SP_CLIENT_ID>`;
```

The end user must have permission to authenticate to the workspace and
authorize the requested `sql` OAuth scope on first load.

## File layout

```
governance-tagger/
├── README.md              # this file
├── ARCHITECTURE.md        # diagrams + components
├── LOGGING.md             # the reusable security-logging pattern
├── LICENSE                # Apache 2.0
├── databricks.yml         # Databricks Asset Bundle root
├── resources/
│   └── governance_tagger.app.yml   # App resource definition
├── manifest.yaml          # App manifest (SQL warehouse resource_spec)
├── app.yaml               # App runtime config (command, env vars)
├── requirements.txt       # Python deps
├── app.py                 # FastAPI entry point (lifespan + SPA mount)
├── server/
│   ├── config.py          # auth helpers + env config
│   ├── audit_logger/      # reusable two-tier audit logger (drop-in package)
│   ├── uc.py              # thin Unity Catalog accessor
│   └── routes/api.py      # /api/* HTTP routes
└── frontend/              # React + Vite SPA
    ├── package.json
    ├── vite.config.ts
    ├── tsconfig.json
    ├── index.html
    ├── src/               # editable source
    │   ├── App.tsx
    │   ├── main.tsx
    │   └── styles.css
    └── dist/              # built bundle (committed so DAB deploy needs no npm)
```

## See also

- `ARCHITECTURE.md` — diagrams of the full request flow.
- `LOGGING.md` — the reusable security-logging design pattern. **Read this**
  if you are building another app and want to drop in the same audit
  pipeline.
