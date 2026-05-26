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

## Deploy from this directory

```bash
# 1. Authenticate
databricks auth login --profile <your_databricks_profile>

# 2. Build the React frontend (output goes to frontend/dist)
cd frontend && npm install && npm run build && cd ..

# 3. Sync to the workspace (skip node_modules, venv, frontend sources)
databricks sync . /Workspace/Users/$USER/governance-tagger \
  --exclude node_modules --exclude .venv --exclude __pycache__ \
  --exclude .git --exclude frontend/src --exclude frontend/public \
  --exclude frontend/node_modules --full \
  --profile <your_databricks_profile>

# 4. Create the app (first time only)
databricks apps create governance-tagger \
  --description "Governance team UI to curate UC table and column descriptions, with centralized audit logging." \
  --profile <your_databricks_profile>

# 5. Deploy
databricks apps deploy governance-tagger \
  --source-code-path /Workspace/Users/$USER/governance-tagger \
  --profile <your_databricks_profile>
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
├── app.yaml               # Databricks App runtime config
├── requirements.txt       # Python deps
├── app.py                 # FastAPI entry point (lifespan + SPA mount)
├── server/
│   ├── config.py          # auth helpers + env config
│   ├── security_log.py    # reusable two-tier audit logger
│   ├── uc.py              # thin Unity Catalog accessor
│   └── routes/api.py      # /api/* HTTP routes
├── frontend/              # React + Vite SPA (source)
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   └── src/
│       ├── App.tsx        # single-page UI
│       ├── main.tsx       # React root
│       └── styles.css     # Databricks-styled CSS
└── screenshots/           # demo screenshots from the verification run
```

## See also

- `ARCHITECTURE.md` — diagrams of the full request flow.
- `LOGGING.md` — the reusable security-logging design pattern. **Read this**
  if you are building another app and want to drop in the same audit
  pipeline.
