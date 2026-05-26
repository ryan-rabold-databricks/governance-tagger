import { useEffect, useState } from 'react';

type Me = { email: string };
type TableRef = { catalog: string; schema: string; table: string; comment: string };
type Column = { name: string; type: string; comment: string; position: number };
type TableInfo = { catalog: string; schema: string; table: string; comment: string; owner: string; columns: Column[] };
type AuditEvent = {
  timestamp: string; event_type: string; outcome: string; action: string;
  catalog: string; schema: string; table: string; column: string;
  before: string | null; after: string | null; error: string | null;
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) } });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return (await res.json()) as T;
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [catalogs, setCatalogs] = useState<string[]>([]);
  const [catalog, setCatalog] = useState<string>('');
  const [schemas, setSchemas] = useState<string[]>([]);
  const [schema, setSchema] = useState<string>('');
  const [tables, setTables] = useState<TableRef[]>([]);
  const [selected, setSelected] = useState<TableInfo | null>(null);
  const [tableDesc, setTableDesc] = useState('');
  const [colDrafts, setColDrafts] = useState<Record<string, string>>({});
  const [savingDesc, setSavingDesc] = useState(false);
  const [savingCol, setSavingCol] = useState<string | null>(null);
  const [descStatus, setDescStatus] = useState<{ kind: 'success' | 'error' | 'dirty'; msg: string } | null>(null);
  const [colStatus, setColStatus] = useState<Record<string, { kind: 'success' | 'error' | 'dirty'; msg: string }>>({});
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [banner, setBanner] = useState<string | null>(null);

  useEffect(() => {
    api<Me>('/api/me').then(setMe).catch(e => setBanner(`Could not load identity: ${e.message}`));
    // Populate the catalog list but do NOT auto-select — the user must
    // explicitly pick a catalog before the schema dropdown unlocks.
    api<{ catalogs: string[] }>('/api/catalogs')
      .then(r => setCatalogs(r.catalogs))
      .catch(e => setBanner(`Could not load catalogs: ${e.message}`));
    refreshAudit();
  }, []);

  // Catalog changes → reload schemas (filtered to that catalog server-side);
  // clear all downstream state so stale schemas/tables never linger.
  useEffect(() => {
    setSchemas([]); setSchema(''); setTables([]); setSelected(null);
    if (!catalog) return;
    api<{ schemas: string[] }>(`/api/catalogs/${encodeURIComponent(catalog)}/schemas`)
      .then(r => setSchemas(r.schemas))
      .catch(e => setBanner(`Could not load schemas: ${e.message}`));
  }, [catalog]);

  // Schema changes → reload tables; clear table selection.
  useEffect(() => {
    if (!catalog || !schema) {
      setTables([]); setSelected(null);
      return;
    }
    setSelected(null);
    api<{ tables: TableRef[] }>(
      `/api/catalogs/${encodeURIComponent(catalog)}/schemas/${encodeURIComponent(schema)}/tables`
    )
      .then(r => setTables(r.tables))
      .catch(e => setBanner(`Could not load tables: ${e.message}`));
  }, [catalog, schema]);

  function selectTable(t: TableRef) {
    api<TableInfo>(`/api/tables/${t.catalog}/${t.schema}/${t.table}`).then(info => {
      setSelected(info);
      setTableDesc(info.comment);
      const drafts: Record<string, string> = {};
      info.columns.forEach(c => { drafts[c.name] = c.comment; });
      setColDrafts(drafts);
      setDescStatus(null);
      setColStatus({});
    }).catch(e => setBanner(`Could not load table: ${e.message}`));
  }

  function refreshAudit() {
    api<{ events: AuditEvent[] }>('/api/audit/mine?limit=15').then(r => setAudit(r.events)).catch(() => {});
  }

  async function saveDescription() {
    if (!selected) return;
    setSavingDesc(true);
    setDescStatus(null);
    try {
      await api(`/api/tables/${selected.catalog}/${selected.schema}/${selected.table}/description`, {
        method: 'PUT',
        body: JSON.stringify({ comment: tableDesc }),
      });
      setDescStatus({ kind: 'success', msg: 'Saved' });
      setSelected({ ...selected, comment: tableDesc });
      setTimeout(refreshAudit, 1500);
    } catch (e: any) {
      setDescStatus({ kind: 'error', msg: e.message });
    } finally {
      setSavingDesc(false);
    }
  }

  async function saveColumn(name: string) {
    if (!selected) return;
    setSavingCol(name);
    setColStatus({ ...colStatus, [name]: { kind: 'dirty', msg: 'Saving…' } });
    try {
      await api(`/api/tables/${selected.catalog}/${selected.schema}/${selected.table}/columns`, {
        method: 'PUT',
        body: JSON.stringify({ column: name, comment: colDrafts[name] || '' }),
      });
      setColStatus({ ...colStatus, [name]: { kind: 'success', msg: 'Saved' } });
      setSelected({
        ...selected,
        columns: selected.columns.map(c => c.name === name ? { ...c, comment: colDrafts[name] || '' } : c),
      });
      setTimeout(refreshAudit, 1500);
    } catch (e: any) {
      setColStatus({ ...colStatus, [name]: { kind: 'error', msg: e.message } });
    } finally {
      setSavingCol(null);
    }
  }

  const descDirty = selected && tableDesc !== selected.comment;

  return (
    <div className="app-shell">
      <div className="app-header">
        <div className="app-title">
          <h1>Governance Tagger</h1>
          <span className="subtitle">Curate table and column descriptions across your data estate.</span>
        </div>
        <div className="user-chip">{me?.email ?? '…'}</div>
      </div>

      {banner && <div className="banner error">{banner}</div>}

      <div className="grid">
        <div className="card">
          <h2>Browse</h2>
          <label htmlFor="catalog">Catalog</label>
          <select id="catalog" value={catalog} onChange={e => setCatalog(e.target.value)}>
            <option value="">
              {catalogs.length === 0 ? '(no catalogs visible)' : 'Select a catalog…'}
            </option>
            {catalogs.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
          <label htmlFor="schema" style={{ marginTop: 12 }}>Schema</label>
          <select
            id="schema"
            value={schema}
            onChange={e => setSchema(e.target.value)}
            disabled={!catalog}
          >
            <option value="">
              {!catalog
                ? '— pick a catalog first —'
                : schemas.length === 0
                  ? '(no schemas in this catalog)'
                  : 'Select a schema…'}
            </option>
            {schemas.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
          <ul className="tables-list">
            {tables.length === 0 && (
              <div className="empty">
                {!catalog
                  ? 'Pick a catalog to start.'
                  : !schema
                    ? 'Pick a schema to see its tables.'
                    : 'No tables in this schema.'}
              </div>
            )}
            {tables.map(t => (
              <li
                key={`${t.schema}.${t.table}`}
                className={selected && selected.schema === t.schema && selected.table === t.table ? 'active' : ''}
                onClick={() => selectTable(t)}
              >
                <div className="table-name">{t.table}</div>
                <div className="table-path">{t.catalog}.{t.schema}</div>
              </li>
            ))}
          </ul>
        </div>

        <div className="card">
          {!selected && <div className="empty">Pick a table on the left to view and edit its description.</div>}
          {selected && (
            <>
              <h2>{selected.schema}.{selected.table}</h2>
              <p style={{ fontSize: 12, color: 'var(--muted)', marginTop: -8 }}>Owner: {selected.owner}</p>

              <label htmlFor="desc" style={{ marginTop: 16 }}>Table description</label>
              <textarea id="desc" value={tableDesc} onChange={e => setTableDesc(e.target.value)} />
              <div className="save-row">
                <button onClick={saveDescription} disabled={savingDesc || !descDirty}>
                  {savingDesc ? 'Saving…' : 'Save description'}
                </button>
                {descStatus && <span className={`status ${descStatus.kind}`}>{descStatus.msg}</span>}
                {!descStatus && descDirty && <span className="status dirty">Unsaved changes</span>}
              </div>

              <h2 style={{ marginTop: 24 }}>Columns</h2>
              <table className="columns-table">
                <thead>
                  <tr><th>Name</th><th>Type</th><th>Comment</th><th /></tr>
                </thead>
                <tbody>
                  {selected.columns.map(c => {
                    const draft = colDrafts[c.name] ?? '';
                    const dirty = draft !== c.comment;
                    const st = colStatus[c.name];
                    return (
                      <tr key={c.name}>
                        <td className="col-name">{c.name}</td>
                        <td className="col-type">{c.type}</td>
                        <td>
                          <input
                            type="text"
                            value={draft}
                            onChange={e => setColDrafts({ ...colDrafts, [c.name]: e.target.value })}
                            placeholder="(no comment)"
                          />
                        </td>
                        <td style={{ minWidth: 140 }}>
                          <button
                            className="ghost"
                            disabled={savingCol === c.name || !dirty}
                            onClick={() => saveColumn(c.name)}
                          >
                            {savingCol === c.name ? 'Saving…' : 'Save'}
                          </button>
                          {st && <div style={{ marginTop: 4 }}><span className={`status ${st.kind}`}>{st.msg}</span></div>}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </>
          )}
        </div>
      </div>

      <div className="card" style={{ marginTop: 20 }}>
        <h2>Your recent activity</h2>
        <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: -4 }}>
          Every edit you make is recorded in the central audit log.
          <button className="ghost" style={{ marginLeft: 8 }} onClick={refreshAudit}>Refresh</button>
        </p>
        {audit.length === 0 && <div className="empty">No activity yet.</div>}
        <ul className="audit-list">
          {audit.map((e, i) => (
            <li key={i} className={e.outcome !== 'success' ? 'failure' : ''}>
              <div className="timestamp">{e.timestamp}</div>
              <div><span className="audit-action">{e.action}</span> · <span className={`status ${e.outcome === 'success' ? 'success' : 'error'}`}>{e.outcome}</span></div>
              {(e.catalog || e.schema || e.table) && (
                <div className="audit-target">{[e.catalog, e.schema, e.table, e.column].filter(Boolean).join('.')}</div>
              )}
              {(e.before !== null || e.after !== null) && (
                <div className="audit-delta">
                  <strong>Before:</strong> {e.before || <em>(empty)</em>}<br />
                  <strong>After:</strong> {e.after || <em>(empty)</em>}
                </div>
              )}
              {e.error && <div className="audit-delta" style={{ color: 'var(--danger)' }}>{e.error}</div>}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
