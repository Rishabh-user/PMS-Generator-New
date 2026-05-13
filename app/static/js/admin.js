/* ================================================================
   Admin · PMS Database — table browser frontend.
   Talks to the same backend's /api/admin endpoints.
   ================================================================ */

const PAGE_SIZE = 50;

const API = {
    tables: () => fetch('/api/admin/tables'),
    rows:   (name, limit, offset) =>
        fetch(`/api/admin/tables/${encodeURIComponent(name)}/rows?limit=${limit}&offset=${offset}`),
    deleteRow: (name, keyBody) =>
        fetch(`/api/admin/tables/${encodeURIComponent(name)}/rows`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(keyBody),
        }),
};

/* The admin /api/admin/tables response carries each table's primary-key
   columns inside the columns metadata. To keep the JS independent of
   server-side detection, we hardcode the PK column lookup against the
   known tables we ship. For arbitrary tables the user adds later we
   default to assuming an `id` column, which covers most cases.        */
const TABLE_PK_OVERRIDES = {
    pms_agent_sessions: ['id', 'user_id'], // composite primary key
};

function pkColumnsFor(tableName, columns) {
    if (TABLE_PK_OVERRIDES[tableName]) return TABLE_PK_OVERRIDES[tableName];
    if (columns && columns.includes('id')) return ['id'];
    return null; // unknown PK shape — delete button hidden for this table
}

const state = {
    tables: [],
    activeName: null,
    rowsData: null,    // last fetched rows payload
    offset: 0,
    filter: '',
    showRaw: false,
};

// ----------------------------------------------------------------
// Toasts
// ----------------------------------------------------------------
function toast(msg, kind = 'info', timeout = 3000) {
    const root = document.getElementById('adminToastContainer');
    if (!root) return;
    const el = document.createElement('div');
    el.className = `admin-toast ${kind}`;
    el.textContent = msg;
    root.appendChild(el);
    setTimeout(() => {
        el.style.transition = 'opacity 0.2s ease';
        el.style.opacity = '0';
        setTimeout(() => el.remove(), 220);
    }, timeout);
}

// ----------------------------------------------------------------
// API badge — green when /api/admin/tables responds, red on error.
// ----------------------------------------------------------------
function markApiOnline()  { document.getElementById('apiBadge')?.classList.remove('offline'); document.getElementById('apiBadge')?.classList.add('online'); }
function markApiOffline() { document.getElementById('apiBadge')?.classList.remove('online'); document.getElementById('apiBadge')?.classList.add('offline'); }

// ----------------------------------------------------------------
// HTML-escape a value for safe insertion via innerHTML. Used only
// when we deliberately build small bits of markup — most cells use
// textContent / DOM nodes directly.
// ----------------------------------------------------------------
function esc(v) {
    if (v == null) return '';
    return String(v)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// ----------------------------------------------------------------
// Tables list
// ----------------------------------------------------------------
async function loadTables() {
    const listEl  = document.getElementById('tablesList');
    const errEl   = document.getElementById('tablesError');
    const countEl = document.getElementById('tablesCount');
    listEl.innerHTML = `<li class="admin-rail-empty">Loading tables…</li>`;
    errEl.hidden = true;
    countEl.textContent = '—';

    try {
        const res = await API.tables();
        if (!res.ok) {
            // Pull detail out of the JSON error envelope when present.
            let detail = `HTTP ${res.status}`;
            try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
            throw new Error(detail);
        }
        const tables = await res.json();
        state.tables = tables;
        countEl.textContent = String(tables.length);
        markApiOnline();
        renderTablesList();
        // Auto-select the first table on initial load.
        if (!state.activeName && tables.length > 0) {
            selectTable(tables[0].name);
        }
    } catch (e) {
        console.error('[admin] loadTables failed:', e);
        markApiOffline();
        listEl.innerHTML = '';
        errEl.hidden = false;
        errEl.textContent = e.message || 'Failed to load tables.';
    }
}

function renderTablesList() {
    const listEl = document.getElementById('tablesList');
    if (state.tables.length === 0) {
        listEl.innerHTML = `<li class="admin-rail-empty">No tables in the public schema.</li>`;
        return;
    }
    listEl.innerHTML = '';
    for (const t of state.tables) {
        const li = document.createElement('li');
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'admin-rail-item';
        if (t.name === state.activeName) btn.classList.add('active');
        btn.innerHTML = `
            <div class="admin-rail-row1">
                <span class="admin-rail-name">${esc(t.name)}</span>
                <span class="admin-rail-rows">${t.row_count.toLocaleString()}</span>
            </div>
            <div class="admin-rail-cols">${t.columns.length} columns</div>
        `;
        btn.addEventListener('click', () => selectTable(t.name));
        li.appendChild(btn);
        listEl.appendChild(li);
    }
}

function selectTable(name) {
    state.activeName = name;
    state.offset = 0;
    state.filter = '';
    state.showRaw = false;
    document.getElementById('filterInput').value = '';
    document.getElementById('rawToggleBtn').classList.remove('active');
    renderTablesList();
    document.getElementById('adminEmpty').hidden = true;
    document.getElementById('adminViewer').hidden = false;
    loadRows();
}

// ----------------------------------------------------------------
// Rows for the active table
// ----------------------------------------------------------------
async function loadRows() {
    if (!state.activeName) return;

    const loadingEl = document.getElementById('rowsLoading');
    const errorEl   = document.getElementById('rowsError');
    const emptyEl   = document.getElementById('rowsEmpty');
    const rawEl     = document.getElementById('rowsRaw');
    const tableWrap = document.getElementById('rowsTableWrap');
    const filterNoteEl = document.getElementById('filterNote');

    loadingEl.hidden = false;
    errorEl.hidden = true;
    emptyEl.hidden = true;
    rawEl.hidden = true;
    tableWrap.hidden = true;
    filterNoteEl.hidden = true;

    try {
        const res = await API.rows(state.activeName, PAGE_SIZE, state.offset);
        if (!res.ok) {
            let detail = `HTTP ${res.status}`;
            try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
            throw new Error(detail);
        }
        const data = await res.json();
        state.rowsData = data;
        renderMeta();
        renderPager();
        renderRows();
    } catch (e) {
        console.error('[admin] loadRows failed:', e);
        errorEl.hidden = false;
        errorEl.textContent = e.message || 'Failed to load rows.';
        state.rowsData = null;
        renderMeta();
        renderPager();
    } finally {
        loadingEl.hidden = true;
    }
}

function renderMeta() {
    const tableMeta = state.tables.find(t => t.name === state.activeName);
    document.getElementById('metaTableName').textContent = state.activeName || '—';
    document.getElementById('metaRowCount').textContent =
        state.rowsData ? state.rowsData.total.toLocaleString() : (tableMeta ? tableMeta.row_count.toLocaleString() : '—');
    document.getElementById('metaColumnCount').textContent =
        tableMeta ? String(tableMeta.columns.length) : '—';
}

function renderPager() {
    const data = state.rowsData;
    const total = data ? data.total : 0;
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    const page  = Math.floor(state.offset / PAGE_SIZE) + 1;
    document.getElementById('pagerLabel').textContent = `Page ${page} / ${pages}`;
    document.getElementById('prevPageBtn').disabled = state.offset <= 0;
    document.getElementById('nextPageBtn').disabled = !data || state.offset + PAGE_SIZE >= total;
}

function filteredRows() {
    const data = state.rowsData;
    if (!data) return [];
    if (!state.filter) return data.rows;
    const q = state.filter.toLowerCase();
    return data.rows.filter(r => JSON.stringify(r).toLowerCase().includes(q));
}

function renderRows() {
    const data = state.rowsData;
    const tableWrap = document.getElementById('rowsTableWrap');
    const rawEl     = document.getElementById('rowsRaw');
    const emptyEl   = document.getElementById('rowsEmpty');
    const filterNoteEl = document.getElementById('filterNote');

    if (!data) return;

    if (data.rows.length === 0) {
        emptyEl.hidden = false;
        return;
    }
    emptyEl.hidden = true;

    const rows = filteredRows();

    if (state.showRaw) {
        rawEl.hidden = false;
        tableWrap.hidden = true;
        rawEl.textContent = JSON.stringify(rows, null, 2);
    } else {
        rawEl.hidden = true;
        tableWrap.hidden = false;
        renderTable(data.columns, rows);
    }

    if (state.filter && rows.length !== data.rows.length) {
        filterNoteEl.hidden = false;
        filterNoteEl.textContent =
            `Filtered ${rows.length} of ${data.rows.length} rows on this page (server total: ${data.total.toLocaleString()}).`;
    } else {
        filterNoteEl.hidden = true;
    }
}

function renderTable(columns, rows) {
    const head = document.getElementById('rowsTableHead');
    const body = document.getElementById('rowsTableBody');
    head.innerHTML = '';
    body.innerHTML = '';
    for (const col of columns) {
        const th = document.createElement('th');
        th.textContent = col;
        head.appendChild(th);
    }
    // Trailing actions column (Delete) when we know how to identify
    // rows in this table (i.e. we have a primary-key column set).
    const pkCols = pkColumnsFor(state.activeName, columns);
    if (pkCols) {
        const th = document.createElement('th');
        th.textContent = '';
        th.style.width = '60px';
        head.appendChild(th);
    }
    for (const r of rows) {
        const tr = document.createElement('tr');
        for (const col of columns) {
            tr.appendChild(buildCell(r[col]));
        }
        if (pkCols) {
            tr.appendChild(buildDeleteCell(r, pkCols));
        }
        body.appendChild(tr);
    }
}

function buildDeleteCell(row, pkCols) {
    const td = document.createElement('td');
    td.style.textAlign = 'center';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'admin-row-delete';
    btn.title = 'Delete this row';
    btn.innerHTML = `
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14H7L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a2 2 0 012-2h2a2 2 0 012 2v2"/></svg>
    `;
    btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        handleDeleteRow(row, pkCols, btn);
    });
    td.appendChild(btn);
    return td;
}

async function handleDeleteRow(row, pkCols, btn) {
    // Build the natural-language "id=5" / "id=5, user_id=abc" label
    // so the confirm dialog tells the user exactly which row they're
    // about to drop.
    const keyParts = pkCols.map(c => `${c}=${row[c]}`).join(', ');
    if (!window.confirm(`Delete this row?\n\n${keyParts}\n\nThis cannot be undone.`)) {
        return;
    }
    const keyBody = {};
    for (const c of pkCols) keyBody[c] = row[c];

    btn.disabled = true;
    try {
        const res = await API.deleteRow(state.activeName, keyBody);
        if (!res.ok) {
            let detail = `HTTP ${res.status}`;
            try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
            throw new Error(detail);
        }
        toast('Row deleted', 'success');
        // Refresh the current page; if we just deleted the last row on a
        // page, walk back to the previous page automatically.
        await loadRows();
        if (state.rowsData && state.rowsData.rows.length === 0 && state.offset > 0) {
            state.offset = Math.max(0, state.offset - PAGE_SIZE);
            await loadRows();
        }
        // Also refresh the rail row counts.
        loadTables();
    } catch (e) {
        console.error('[admin] delete failed:', e);
        toast(`Delete failed: ${e.message || e}`, 'error', 4500);
    } finally {
        btn.disabled = false;
    }
}

// Build a single <td>. Heuristics:
//   • null / undefined  → italic muted "null"
//   • boolean           → mono blue
//   • number            → mono right-tinted
//   • string ≤ 200 chars → plain text
//   • string > 200 chars → truncated preview with full text in title=""
//   • array / object    → <details> disclosure with JSON
function buildCell(value) {
    const td = document.createElement('td');

    if (value === null || value === undefined) {
        td.innerHTML = '<span class="admin-cell-null">null</span>';
        return td;
    }
    if (typeof value === 'boolean') {
        td.innerHTML = `<span class="admin-cell-bool">${value ? 'true' : 'false'}</span>`;
        return td;
    }
    if (typeof value === 'number') {
        td.innerHTML = `<span class="admin-cell-num">${value}</span>`;
        return td;
    }
    if (typeof value === 'string') {
        if (value.length > 200) {
            const span = document.createElement('span');
            span.className = 'admin-cell-truncated';
            span.title = value;
            span.textContent = value.slice(0, 200);
            td.appendChild(span);
        } else {
            td.textContent = value;
        }
        return td;
    }
    // arrays / objects (JSONB columns come back already parsed)
    const det = document.createElement('details');
    det.className = 'admin-json-disclosure';
    const sum = document.createElement('summary');
    sum.textContent = Array.isArray(value)
        ? `[${value.length} items]`
        : `{${Object.keys(value).length} keys}`;
    det.appendChild(sum);
    const pre = document.createElement('pre');
    pre.className = 'admin-json-body';
    pre.textContent = JSON.stringify(value, null, 2);
    det.appendChild(pre);
    td.appendChild(det);
    return td;
}

// ----------------------------------------------------------------
// Export current-page rows (after client-side filter) as JSON.
// ----------------------------------------------------------------
function exportRows() {
    if (!state.rowsData) return;
    const rows = filteredRows();
    const blob = new Blob([JSON.stringify(rows, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    const page = Math.floor(state.offset / PAGE_SIZE) + 1;
    a.download = `${state.activeName}_page${page}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast(`Exported ${rows.length} row${rows.length === 1 ? '' : 's'}`, 'success');
}

// ----------------------------------------------------------------
// Wire up controls
// ----------------------------------------------------------------
function wireEvents() {
    document.getElementById('reloadTablesBtn').addEventListener('click', () => loadTables().then(() => state.activeName && loadRows()));
    document.getElementById('filterInput').addEventListener('input', (e) => {
        state.filter = e.target.value || '';
        renderRows();
    });
    document.getElementById('exportBtn').addEventListener('click', exportRows);
    document.getElementById('rawToggleBtn').addEventListener('click', (e) => {
        state.showRaw = !state.showRaw;
        e.currentTarget.classList.toggle('active', state.showRaw);
        renderRows();
    });
    document.getElementById('prevPageBtn').addEventListener('click', () => {
        if (state.offset <= 0) return;
        state.offset = Math.max(0, state.offset - PAGE_SIZE);
        loadRows();
    });
    document.getElementById('nextPageBtn').addEventListener('click', () => {
        if (!state.rowsData) return;
        if (state.offset + PAGE_SIZE >= state.rowsData.total) return;
        state.offset += PAGE_SIZE;
        loadRows();
    });
}

// ----------------------------------------------------------------
// Bootstrap
// ----------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    wireEvents();
    loadTables();
});
