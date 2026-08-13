/* ================================================================
   AI Settings — admin page for /api/ai-settings.
   Lets an admin save several AI providers (Anthropic / OpenAI /
   OpenAI-compatible) and pick which one is active. A new/edited
   provider is verified with a real test call server-side before it's
   ever saved or activated, so a bad key is rejected here rather than
   breaking the chat / AI notes features.
   ================================================================ */

const PROVIDER_LABEL = {
    anthropic: 'Anthropic (Claude)',
    openai: 'OpenAI (ChatGPT)',
    openai_compatible: 'OpenAI-compatible',
};

// The dropdown IS the provider list this page offers — "pick the
// engine, paste its key". Model/base_url are wire-format details most
// admins never need to see, so they're pre-filled here and tucked
// behind "Show advanced" for anything other than Custom. The backend
// runs a real test call before anything is saved, so a stale
// model/URL here just fails that check rather than silently breaking
// the chat.
const PRESETS = [
    {
        key: 'anthropic',
        label: 'Anthropic — Claude Sonnet 4.6',
        provider: 'anthropic',
        model: 'claude-sonnet-4-6',
        baseUrl: '',
        defaultLabel: 'Anthropic (Claude Sonnet 4.6)',
    },
    {
        key: 'openai',
        label: 'OpenAI — ChatGPT (GPT-4o)',
        provider: 'openai',
        model: 'gpt-4o',
        baseUrl: '',
        defaultLabel: 'OpenAI (GPT-4o)',
    },
    {
        key: 'qwen',
        label: 'Qwen — Qwen2.5 VL 72B Instruct',
        provider: 'openai_compatible',
        model: 'qwen2.5-vl-72b-instruct',
        baseUrl: 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
        defaultLabel: 'Qwen2.5 VL 72B Instruct (DashScope)',
        note: "Via Alibaba DashScope's OpenAI-compatible endpoint.",
    },
    {
        key: 'mimo',
        label: 'Xiaomi — MiMo-V2.5 (OpenRouter)',
        provider: 'openai_compatible',
        model: 'xiaomi/mimo-v2.5',
        baseUrl: 'https://openrouter.ai/api/v1',
        defaultLabel: 'Xiaomi MiMo-V2.5 (OpenRouter)',
        note: "Via OpenRouter's OpenAI-compatible endpoint.",
    },
    {
        key: 'gemma4',
        label: 'Google — Gemma 4 31B (OpenRouter)',
        provider: 'openai_compatible',
        model: 'google/gemma-4-31b-it',
        baseUrl: 'https://openrouter.ai/api/v1',
        defaultLabel: 'Gemma 4 31B (OpenRouter)',
        note: 'Via OpenRouter by default — edit the endpoint below to point at your own self-hosted vLLM server instead.',
    },
    {
        key: 'custom',
        label: 'Custom / other engine',
        provider: 'openai_compatible',
        model: '',
        baseUrl: '',
        defaultLabel: '',
        note: 'Any other OpenAI-compatible endpoint — enter its model id and base URL yourself.',
    },
];

const API = {
    list: () => fetch('/api/ai-settings'),
    create: (body) => fetch('/api/ai-settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }),
    update: (id, body) => fetch(`/api/ai-settings/${id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }),
    activate: (id) => fetch(`/api/ai-settings/${id}/activate`, { method: 'POST' }),
    remove: (id) => fetch(`/api/ai-settings/${id}`, { method: 'DELETE' }),
};

const state = {
    data: null,        // last /api/ai-settings response
    formMode: null,     // null | 'add' | 'edit'
    editingConfig: null,
    rowBusyId: null,
};

// ----------------------------------------------------------------
// Toasts (shared markup with /admin)
// ----------------------------------------------------------------
function toast(msg, kind = 'info', timeout = 3500) {
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

function markApiOnline()  { document.getElementById('apiBadge')?.classList.remove('offline'); document.getElementById('apiBadge')?.classList.add('online'); }
function markApiOffline() { document.getElementById('apiBadge')?.classList.remove('online'); document.getElementById('apiBadge')?.classList.add('offline'); }

function esc(v) {
    if (v == null) return '';
    return String(v)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

async function _errorDetail(resp) {
    try {
        const j = await resp.json();
        return j.detail || `HTTP ${resp.status}`;
    } catch {
        return `HTTP ${resp.status}`;
    }
}

// ----------------------------------------------------------------
// Load + render
// ----------------------------------------------------------------
async function loadSettings() {
    const loadingEl = document.getElementById('aisLoading');
    const cardEl = document.getElementById('aisCard');
    const errEl = document.getElementById('aisError');
    loadingEl.hidden = false;
    cardEl.hidden = true;
    errEl.hidden = true;
    try {
        const resp = await API.list();
        if (!resp.ok) throw new Error(await _errorDetail(resp));
        state.data = await resp.json();
        markApiOnline();
        renderCard();
        cardEl.hidden = false;
    } catch (e) {
        markApiOffline();
        errEl.textContent = `Couldn't load AI settings: ${e.message}`;
        errEl.hidden = false;
    } finally {
        loadingEl.hidden = true;
    }
}

function renderCard() {
    const d = state.data;
    const count = d.configs.length;
    document.getElementById('aisCount').textContent =
        `${count} saved provider${count === 1 ? '' : 's'}`;

    const subEl = document.getElementById('aisSubtitle');
    if (d.active_id === null) {
        subEl.hidden = false;
        subEl.textContent = d.env_fallback_available
            ? "None active — using the server's .env ANTHROPIC_API_KEY fallback."
            : 'None active, and no .env fallback is configured.';
    } else {
        subEl.hidden = true;
    }

    const emptyEl = document.getElementById('aisEmpty');
    const wrapEl = document.getElementById('aisTableWrap');
    if (count === 0) {
        emptyEl.hidden = false;
        wrapEl.hidden = true;
        return;
    }
    emptyEl.hidden = true;
    wrapEl.hidden = false;

    const tbody = document.getElementById('aisTableBody');
    tbody.innerHTML = d.configs.map(renderRow).join('');

    tbody.querySelectorAll('[data-activate]').forEach((btn) => {
        btn.addEventListener('click', () => activateProvider(Number(btn.dataset.activate)));
    });
    tbody.querySelectorAll('[data-edit]').forEach((btn) => {
        btn.addEventListener('click', () => {
            const cfg = d.configs.find((c) => c.id === Number(btn.dataset.edit));
            if (cfg) openEditForm(cfg);
        });
    });
    tbody.querySelectorAll('[data-delete]').forEach((btn) => {
        btn.addEventListener('click', () => removeProvider(Number(btn.dataset.delete)));
    });
}

function renderRow(c) {
    const busy = state.rowBusyId === c.id;
    const statusCell = c.is_active
        ? `<span class="ais-badge-active">
             <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"><path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="9"/></svg>
             active
           </span>`
        : `<button type="button" class="admin-btn ais-activate-btn" data-activate="${c.id}" ${busy ? 'disabled' : ''}>
             ${busy ? '<span class="ais-spin ais-spin-dark"></span>' : ''}Activate
           </button>`;

    return `
        <tr>
            <td><strong>${esc(c.label)}</strong></td>
            <td>${esc(PROVIDER_LABEL[c.provider] || c.provider)}</td>
            <td class="mono">${esc(c.model || '—')}</td>
            <td class="mono">${esc(c.api_key_hint)}</td>
            <td>${statusCell}</td>
            <td>
                <div class="ais-row-actions">
                    <button type="button" class="ais-icon-btn" title="Edit" data-edit="${c.id}">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 013 3L7 19l-4 1 1-4z"/></svg>
                    </button>
                    <button type="button" class="ais-icon-btn ais-icon-danger" title="${c.is_active ? 'Activate a different provider first' : 'Remove'}" data-delete="${c.id}" ${c.is_active || busy ? 'disabled' : ''}>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 011-1h4a1 1 0 011 1v2"/></svg>
                    </button>
                </div>
            </td>
        </tr>`;
}

function showRowError(msg) {
    const el = document.getElementById('aisRowError');
    if (!msg) { el.hidden = true; return; }
    el.textContent = msg;
    el.hidden = false;
}

async function activateProvider(id) {
    state.rowBusyId = id;
    showRowError(null);
    renderCard();
    try {
        const resp = await API.activate(id);
        if (!resp.ok) throw new Error(await _errorDetail(resp));
        toast('Provider activated.', 'success');
        await loadSettings();
    } catch (e) {
        showRowError(e.message);
        toast(`Activation failed: ${e.message}`, 'error');
    } finally {
        state.rowBusyId = null;
    }
}

async function removeProvider(id) {
    if (!confirm("Remove this saved AI provider? This can't be undone.")) return;
    state.rowBusyId = id;
    showRowError(null);
    renderCard();
    try {
        const resp = await API.remove(id);
        if (!resp.ok && resp.status !== 204) throw new Error(await _errorDetail(resp));
        toast('Provider removed.', 'success');
        await loadSettings();
    } catch (e) {
        showRowError(e.message);
        toast(`Remove failed: ${e.message}`, 'error');
    } finally {
        state.rowBusyId = null;
    }
}

// ----------------------------------------------------------------
// Add / edit form
// ----------------------------------------------------------------
function populatePresetSelect() {
    const sel = document.getElementById('aisPreset');
    sel.innerHTML = PRESETS.map((p) => `<option value="${p.key}">${esc(p.label)}</option>`).join('');
}

function applyPreset(key) {
    const preset = PRESETS.find((p) => p.key === key) || PRESETS[0];
    document.getElementById('aisLabel').value = preset.defaultLabel;
    document.getElementById('aisModel').value = preset.model;
    document.getElementById('aisBaseUrl').value = preset.baseUrl;
    document.getElementById('aisPresetHint').textContent = preset.note || '';

    const advancedToggle = document.getElementById('aisAdvancedToggle');
    const advanced = document.getElementById('aisAdvanced');
    const baseUrlField = document.getElementById('aisBaseUrlField');
    baseUrlField.hidden = preset.provider === 'anthropic';

    if (preset.key === 'custom') {
        advancedToggle.hidden = true;
        advanced.hidden = false;
    } else {
        advancedToggle.hidden = false;
        advancedToggle.textContent = 'Show advanced (model / endpoint)';
        advanced.hidden = true;
    }
}

function currentPreset() {
    const key = document.getElementById('aisPreset').value;
    return PRESETS.find((p) => p.key === key) || PRESETS[0];
}

function openAddForm() {
    state.formMode = 'add';
    state.editingConfig = null;

    document.getElementById('aisFormTitle').textContent = 'Add provider';
    document.getElementById('aisFormSubtitle').textContent =
        "Pick the AI engine and paste its key — verified with a real request before it's saved.";
    document.getElementById('aisPresetField').hidden = false;
    document.getElementById('aisApiKeyField').hidden = false;
    document.getElementById('aisApiKey').required = true;
    document.getElementById('aisApiKey').value = '';
    document.getElementById('aisApiKeyHint').textContent = 'Only the key is required for a listed engine.';
    document.getElementById('aisActivateRow').hidden = false;
    document.getElementById('aisActivateNow').checked = true;
    document.getElementById('aisSubmitLabel').textContent = 'Save provider';

    populatePresetSelect();
    document.getElementById('aisPreset').value = PRESETS[0].key;
    applyPreset(PRESETS[0].key);
    showFormError(null);
    document.getElementById('aisFormCard').hidden = false;
    document.getElementById('aisFormCard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function openEditForm(cfg) {
    state.formMode = 'edit';
    state.editingConfig = cfg;

    document.getElementById('aisFormTitle').textContent = `Edit "${cfg.label}"`;
    document.getElementById('aisFormSubtitle').textContent =
        'Changes to the key, model, or endpoint are verified with a real request before saving.';
    document.getElementById('aisPresetField').hidden = true;
    document.getElementById('aisLabel').value = cfg.label;
    document.getElementById('aisApiKeyField').hidden = false;
    document.getElementById('aisApiKey').required = false;
    document.getElementById('aisApiKey').value = '';
    document.getElementById('aisApiKey').placeholder = 'Leave blank to keep current key';
    document.getElementById('aisApiKeyHint').textContent = `Leave blank to keep the current key (${cfg.api_key_hint}).`;
    document.getElementById('aisActivateRow').hidden = true;

    document.getElementById('aisModel').value = cfg.model || '';
    document.getElementById('aisBaseUrl').value = cfg.base_url || '';
    document.getElementById('aisBaseUrlField').hidden = cfg.provider === 'anthropic';
    document.getElementById('aisAdvancedToggle').hidden = true;
    document.getElementById('aisAdvanced').hidden = false;
    document.getElementById('aisSubmitLabel').textContent = 'Save changes';

    showFormError(null);
    document.getElementById('aisFormCard').hidden = false;
    document.getElementById('aisFormCard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function closeForm() {
    state.formMode = null;
    state.editingConfig = null;
    document.getElementById('aisFormCard').hidden = true;
}

function showFormError(msg) {
    const el = document.getElementById('aisFormError');
    if (!msg) { el.hidden = true; return; }
    el.textContent = msg;
    el.hidden = false;
}

function setSubmitting(isSubmitting) {
    const btn = document.getElementById('aisSubmitBtn');
    const label = document.getElementById('aisSubmitLabel');
    btn.disabled = isSubmitting;
    document.getElementById('aisCancelBtn').disabled = isSubmitting;
    label.innerHTML = isSubmitting
        ? '<span class="ais-spin"></span>Verifying…'
        : (state.formMode === 'edit' ? 'Save changes' : 'Save provider');
}

async function submitForm(e) {
    e.preventDefault();
    showFormError(null);
    setSubmitting(true);
    try {
        if (state.formMode === 'add') {
            const preset = currentPreset();
            const label = document.getElementById('aisLabel').value.trim();
            const apiKey = document.getElementById('aisApiKey').value;
            const model = document.getElementById('aisModel').value.trim();
            const baseUrl = document.getElementById('aisBaseUrl').value.trim();
            const activateNow = document.getElementById('aisActivateNow').checked;

            if (preset.provider === 'openai_compatible' && !baseUrl) {
                throw new Error('Base URL is required for an OpenAI-compatible provider.');
            }

            const resp = await API.create({
                provider: preset.provider,
                label,
                api_key: apiKey,
                model: model || null,
                base_url: preset.provider === 'anthropic' ? null : (baseUrl || null),
                activate: activateNow,
            });
            if (!resp.ok) throw new Error(await _errorDetail(resp));
            toast('Provider saved.', 'success');
        } else if (state.formMode === 'edit') {
            const cfg = state.editingConfig;
            const label = document.getElementById('aisLabel').value.trim();
            const apiKey = document.getElementById('aisApiKey').value;
            const model = document.getElementById('aisModel').value.trim();
            const baseUrl = document.getElementById('aisBaseUrl').value.trim();

            const newModel = model || null;
            const newBaseUrl = cfg.provider === 'anthropic' ? null : (baseUrl || null);

            if (cfg.provider === 'openai_compatible' && !newBaseUrl) {
                throw new Error('Base URL is required for an OpenAI-compatible provider.');
            }

            const body = { label };
            if (newModel !== (cfg.model ?? null)) { body.model = newModel; body.model_set = true; }
            if (newBaseUrl !== (cfg.base_url ?? null)) { body.base_url = newBaseUrl; body.base_url_set = true; }
            if (apiKey) body.api_key = apiKey;

            const resp = await API.update(cfg.id, body);
            if (!resp.ok) throw new Error(await _errorDetail(resp));
            toast('Provider updated.', 'success');
        }
        closeForm();
        await loadSettings();
    } catch (e) {
        showFormError(e.message);
    } finally {
        setSubmitting(false);
    }
}

// ----------------------------------------------------------------
// Wire up
// ----------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('aisAddBtn').addEventListener('click', () => {
        if (state.formMode === 'add') { closeForm(); return; }
        openAddForm();
    });
    document.getElementById('aisCancelBtn').addEventListener('click', closeForm);
    document.getElementById('aisForm').addEventListener('submit', submitForm);
    document.getElementById('aisPreset').addEventListener('change', (e) => applyPreset(e.target.value));
    document.getElementById('aisAdvancedToggle').addEventListener('click', () => {
        const advanced = document.getElementById('aisAdvanced');
        advanced.hidden = !advanced.hidden;
        document.getElementById('aisAdvancedToggle').textContent =
            advanced.hidden ? 'Show advanced (model / endpoint)' : 'Hide advanced (model / endpoint)';
    });

    loadSettings();
});
