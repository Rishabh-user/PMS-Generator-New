/* PMS Generator (new) — Step 1 only.
 *
 * Pulls all four lists from GET /api/options/all and:
 *   - fills the Pressure Rating / Material / Corrosion Allowance <select>s
 *   - builds the Service multi-select with an "Other (custom)" free-text row
 *   - on submit, gathers the selections and shows a toast with the payload
 *
 * The lists live in app/data/*.json — edit a JSON file and refresh.
 */

const API = {
    options: () => fetch('/api/options/all'),
    resolveClass: (body) => fetch('/api/resolve-class', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    }),
};

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------
function showToast(msg, type = 'info', timeout = 3500) {
    const c = document.getElementById('toastContainer');
    if (!c) return;
    const t = document.createElement('div');
    t.className = `toast ${type}`;
    t.textContent = msg;
    c.appendChild(t);
    setTimeout(() => t.remove(), timeout);
}

function setLoading(on, text = 'Loading...') {
    const overlay = document.getElementById('loadingOverlay');
    document.getElementById('loadingText').textContent = text;
    overlay.classList.toggle('active', !!on);
}

// ---------------------------------------------------------------------------
// API badge (just lights up green once /api/options/all responds)
// ---------------------------------------------------------------------------
function markApiOnline() {
    const b = document.getElementById('apiBadge');
    if (b) b.classList.add('online');
}

// ---------------------------------------------------------------------------
// Plain <select> populator
// ---------------------------------------------------------------------------
function fillSelect(id, items, placeholder) {
    const sel = document.getElementById(id);
    if (!sel) return;
    sel.innerHTML = `<option value="">${placeholder}</option>`;
    items.forEach(v => {
        const opt = document.createElement('option');
        opt.value = v;
        opt.textContent = v;
        sel.appendChild(opt);
    });
}

// ---------------------------------------------------------------------------
// Service multi-select with optional free-text "Other"
// ---------------------------------------------------------------------------
function initServiceMultiSelect(options, allowCustom) {
    const root    = document.getElementById('serviceMultiSelect');
    const panel   = document.getElementById('servicePanel');
    const trigger = document.getElementById('serviceTrigger');
    const label   = document.getElementById('serviceLabel');
    const hidden  = document.getElementById('service');
    const otherIn = document.getElementById('serviceOtherInput');
    if (!root || !panel || !trigger || !hidden) return;

    panel.innerHTML = '';
    options.forEach(opt => {
        const row = document.createElement('label');
        row.className = 'multi-select-option';
        row.dataset.value = opt;
        row.innerHTML = `<input type="checkbox"><span></span>`;
        row.querySelector('input').value = opt;
        row.querySelector('span').textContent = opt;
        panel.appendChild(row);
    });

    if (allowCustom) {
        const div = document.createElement('div');
        div.className = 'multi-select-divider';
        panel.appendChild(div);
        const other = document.createElement('label');
        other.className = 'multi-select-option';
        other.dataset.value = '__OTHER__';
        other.innerHTML = `<input type="checkbox" value="__OTHER__"><span>Other (custom)</span>`;
        panel.appendChild(other);
    }

    function syncValue() {
        const checks = Array.from(panel.querySelectorAll('input[type="checkbox"]'));
        const picks = checks
            .filter(c => c.checked && c.value !== '__OTHER__')
            .map(c => c.value);
        const otherChecked = checks.some(c => c.value === '__OTHER__' && c.checked);
        otherIn.style.display = otherChecked ? 'block' : 'none';
        if (otherChecked) {
            const txt = otherIn.value.trim();
            if (txt) picks.push(txt);
        }
        const joined = picks.join(', ');
        hidden.value = joined;
        if (joined) {
            label.textContent = joined.length > 70 ? joined.slice(0, 67) + '…' : joined;
            label.classList.remove('placeholder');
        } else {
            label.textContent = 'Select one or more services…';
            label.classList.add('placeholder');
        }
        panel.querySelectorAll('.multi-select-option').forEach(r => {
            r.classList.toggle('selected', r.querySelector('input').checked);
        });
    }

    panel.addEventListener('change', e => {
        if (e.target.matches('input[type="checkbox"]')) syncValue();
    });
    otherIn.addEventListener('input', syncValue);

    trigger.addEventListener('click', e => {
        e.stopPropagation();
        const open = root.classList.toggle('open');
        trigger.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('click', e => {
        if (!root.contains(e.target)) {
            root.classList.remove('open');
            trigger.setAttribute('aria-expanded', 'false');
        }
    });
    panel.addEventListener('click', e => e.stopPropagation());
    otherIn.addEventListener('click', e => e.stopPropagation());
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------
async function loadOptions() {
    setLoading(true, 'Loading lists…');
    try {
        const res = await API.options();
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        fillSelect('pipingClass',         data.pressure_ratings,      '-- Select Rating --');
        fillSelect('material',            data.materials,             '-- Select Material --');
        fillSelect('corrosionAllowance',  data.corrosion_allowances,  '-- Select C.A. --');
        initServiceMultiSelect(data.services, !!data.services_allow_custom);
        markApiOnline();
    } catch (e) {
        console.error('[options] failed:', e);
        showToast('Could not load option lists. Check the server.', 'error', 6000);
    } finally {
        setLoading(false);
    }
}

function wireForm() {
    const form = document.getElementById('pmsForm');
    if (!form) return;
    form.addEventListener('submit', e => {
        e.preventDefault();
        const payload = {
            pressure_rating:      document.getElementById('pipingClass').value.trim(),
            material:             document.getElementById('material').value.trim(),
            corrosion_allowance:  document.getElementById('corrosionAllowance').value.trim(),
            service:              document.getElementById('service').value.trim(),
        };
        const missing = Object.entries(payload).filter(([, v]) => !v).map(([k]) => k);
        if (missing.length) {
            showToast(`Please fill: ${missing.join(', ')}`, 'error');
            return;
        }
        console.log('PMS form submitted:', payload);
        showToast(`Selected — ${payload.pressure_rating} | ${payload.material} | ${payload.corrosion_allowance}`, 'success', 5000);
    });
}

// ---------------------------------------------------------------------------
// Class resolution — runs whenever Rating, Material, or CA changes.
// Hides the panel until all three are set; then POSTs to /api/resolve-class
// and renders one of: catalogued / has-variants / derived / error.
// ---------------------------------------------------------------------------
function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;',
    }[c]));
}

function renderResolution(panel, data) {
    panel.style.display = 'block';
    panel.classList.remove('catalogued', 'derived', 'has-variants', 'error');

    let stateClass = 'derived';
    let tagText    = 'Not in Excel — derived';
    if (data.catalogued) {
        stateClass = 'catalogued';
        tagText    = 'In catalogue';
    } else if (data.catalogue_variants && data.catalogue_variants.length) {
        stateClass = 'has-variants';
        tagText    = 'Family — variants exist';
    }
    panel.classList.add(stateClass);

    const matchLine = data.catalogue_match
        ? `<div class="resolution-meta">
              <span><b>Excel rating:</b> ${escapeHtml(data.catalogue_match.rating)}</span>
              <span><b>Excel material:</b> ${escapeHtml(data.catalogue_match.material)}</span>
              <span><b>Excel CA:</b> ${escapeHtml(data.catalogue_match.corrosion_allowance)}</span>
           </div>`
        : '';

    const variantsLine = (data.catalogue_variants && data.catalogue_variants.length)
        ? `<div class="resolution-variants">${
            data.catalogue_variants.map(c => `<span class="resolution-variant-pill">${escapeHtml(c)}</span>`).join('')
          }</div>`
        : '';

    const partsLine = `<div class="resolution-meta">
        <span><b>Letter:</b> ${escapeHtml(data.letter)}</span>
        <span><b>Digit:</b> ${escapeHtml(data.digit)}</span>
        <span><b>Suffix:</b> ${escapeHtml(data.suffix || '—')}</span>
    </div>`;

    panel.innerHTML = `
        <div class="resolution-header">
            <span class="resolution-code">${escapeHtml(data.class_code)}</span>
            <span class="resolution-tag ${stateClass}">${escapeHtml(tagText)}</span>
        </div>
        <div class="resolution-note">${escapeHtml(data.note)}</div>
        ${partsLine}
        ${matchLine}
        ${variantsLine}
    `;
}

function renderResolutionError(panel, message) {
    panel.style.display = 'block';
    panel.classList.remove('catalogued', 'derived', 'has-variants');
    panel.classList.add('error');
    panel.innerHTML = `
        <div class="resolution-header">
            <span class="resolution-tag error">Cannot resolve</span>
        </div>
        <div class="resolution-note">${escapeHtml(message)}</div>
    `;
}

function initClassResolver() {
    const panel = document.getElementById('classResolution');
    const rating   = document.getElementById('pipingClass');
    const material = document.getElementById('material');
    const ca       = document.getElementById('corrosionAllowance');
    const service  = document.getElementById('service');
    if (!panel || !rating || !material || !ca) return;

    // Sequence-protect against fast clicks: only the latest request wins.
    let seq = 0;

    async function tryResolve() {
        const r = rating.value.trim();
        const m = material.value.trim();
        const c = ca.value.trim();
        if (!r || !m || !c) {
            panel.style.display = 'none';
            return;
        }
        const my = ++seq;
        try {
            const res = await API.resolveClass({
                rating: r,
                material: m,
                corrosion_allowance: c,
                service: service ? service.value : '',
            });
            if (my !== seq) return;  // a newer request landed
            const data = await res.json();
            if (!res.ok) {
                renderResolutionError(panel, data.detail || `HTTP ${res.status}`);
                return;
            }
            renderResolution(panel, data);
        } catch (e) {
            if (my !== seq) return;
            renderResolutionError(panel, `Network error: ${e}`);
        }
    }

    [rating, material, ca].forEach(el => el.addEventListener('change', tryResolve));
    // The hidden #service field is updated by the multi-select sync — listen
    // for input on it so service changes also refresh the resolution.
    if (service) service.addEventListener('input', tryResolve);
}

document.addEventListener('DOMContentLoaded', () => {
    loadOptions().then(() => initClassResolver());
    wireForm();
});
