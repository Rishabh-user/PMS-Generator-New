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
    const btn = document.getElementById('generateBtn');
    if (!btn) return;
    btn.addEventListener('click', () => {
        const payload = {
            pressure_rating:      document.getElementById('pipingClass').value.trim(),
            material:             document.getElementById('material').value.trim(),
            corrosion_allowance:  document.getElementById('corrosionAllowance').value.trim(),
            service:              document.getElementById('service').value.trim(),
            design_pressure_barg: parseFloat(document.getElementById('designPressure')?.value) || null,
            design_temperature_c: parseFloat(document.getElementById('designTemperature')?.value) || null,
        };
        const missing = ['pressure_rating', 'material', 'corrosion_allowance', 'service']
            .filter(k => !payload[k]);
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

// Linear interpolation across the P-T curve. Clamps when T is outside the
// indexed range (caller should treat that as a soft warning, but the value
// itself is the closest valid rated point).
function interpolatePressure(temps, pressures, targetT) {
    if (!temps.length || !pressures.length) return null;
    if (targetT <= temps[0]) return pressures[0];
    if (targetT >= temps[temps.length - 1]) return pressures[pressures.length - 1];
    for (let i = 0; i < temps.length - 1; i++) {
        if (targetT >= temps[i] && targetT <= temps[i + 1]) {
            const t1 = temps[i], t2 = temps[i + 1];
            const p1 = pressures[i], p2 = pressures[i + 1];
            return p1 + (p2 - p1) * (targetT - t1) / (t2 - t1);
        }
    }
    return pressures[pressures.length - 1];
}

function renderDesignConditions(pt) {
    // No section when there's no curve to interpolate against.
    if (!pt || pt.pending || !pt.hottest_point) return '';
    const defT = pt.hottest_point.temperature_c;
    const defP = pt.hottest_point.pressure_barg;
    return `
        <div class="design-card">
            <div class="design-card-title">Design conditions <span class="design-card-hint-tag">auto-filled from the P-T table — edit to override</span></div>
            <div class="design-grid">
                <div class="form-group">
                    <label for="designPressure">Design Pressure (barg)</label>
                    <input type="number" id="designPressure" step="0.1" value="${defP}">
                </div>
                <div class="form-group">
                    <label for="designTemperature">Design Temperature (°C)</label>
                    <input type="number" id="designTemperature" step="1" value="${defT}">
                </div>
            </div>
            <p class="design-hint">Defaults to the hottest rated point (most conservative). Change Design Temperature and Design Pressure auto-syncs to the rated value at that temp.</p>
        </div>
    `;
}

function wireDesignInputs(pt) {
    if (!pt || pt.pending) return;
    const tempEl = document.getElementById('designTemperature');
    const presEl = document.getElementById('designPressure');
    if (!tempEl || !presEl) return;
    const temps     = pt.temperatures_c || [];
    const pressures = pt.pressures_barg || [];
    if (!temps.length || !pressures.length) return;

    tempEl.addEventListener('input', () => {
        const t = parseFloat(tempEl.value);
        if (Number.isNaN(t)) return;
        const p = interpolatePressure(temps, pressures, t);
        if (p == null) return;
        presEl.value = (Number.isInteger(p) ? p.toFixed(1) : p.toFixed(2));
    });
}

function renderPtTable(pt) {
    if (!pt) {
        return `
            <div class="pt-card pt-card--missing">
                <div class="pt-card-title">Pressure-Temperature table</div>
                <p class="pt-card-empty">No P-T data indexed for this rating + material combination yet.</p>
            </div>
        `;
    }
    if (pt.pending) {
        return `
            <div class="pt-card pt-card--pending">
                <div class="pt-card-title">Pressure-Temperature table</div>
                <p class="pt-card-empty">${escapeHtml(pt.pending)}</p>
            </div>
        `;
    }

    const head = pt.temperatures_c.map((t, i) => {
        const lbl = (pt.temp_labels && pt.temp_labels[i]) ? pt.temp_labels[i] : String(t);
        return `<th>${escapeHtml(lbl)}</th>`;
    }).join('');
    const body = pt.pressures_barg.map(p => `<td>${escapeHtml(String(p))}</td>`).join('');

    const cold = pt.cold_point;
    const hot  = pt.hottest_point;
    const hint = (cold && hot)
        ? `<p class="pt-hint">Cold-rated point: <strong>${cold.pressure_barg} barg @ ${cold.temperature_c}°C</strong> · Hottest rated: <strong>${hot.pressure_barg} barg @ ${hot.temperature_c}°C</strong></p>`
        : '';

    return `
        <div class="pt-card">
            <div class="pt-card-title">Pressure-Temperature table <span class="pt-group-tag">ASME B16.5 · Group ${escapeHtml(pt.group)}</span></div>
            <div class="pt-table-wrap">
                <table class="pt-mini-table">
                    <thead>
                        <tr>
                            <th class="pt-row-head">Temperature (°C)</th>
                            ${head}
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td class="pt-row-head">Pressure (barg)</td>
                            ${body}
                        </tr>
                    </tbody>
                </table>
            </div>
            ${hint}
        </div>
    `;
}

function renderInputPills(inputs) {
    if (!inputs) return '';
    const { rating, material, ca, service } = inputs;
    const pills = [];

    if (rating)   pills.push(`<span class="pms-banner-tag rating">${escapeHtml(rating)}</span>`);
    if (material) pills.push(`<span class="pms-banner-tag material">${escapeHtml(material)}</span>`);
    if (ca) {
        // "3 mm" → "3 mm CA"; "NIL" stays "NIL" since "NIL CA" is awkward.
        const caLabel = /mm/i.test(ca) ? `${ca} CA` : ca;
        pills.push(`<span class="pms-banner-tag ca">${escapeHtml(caLabel)}</span>`);
    }
    if (service) {
        // Multi-select picker stores values comma-separated. Render each as
        // its own purple pill so a many-services selection stays readable.
        for (const s of service.split(',').map(x => x.trim()).filter(Boolean)) {
            pills.push(`<span class="pms-banner-tag service">${escapeHtml(s)}</span>`);
        }
    }
    return pills.length ? `<div class="resolution-pills">${pills.join('')}</div>` : '';
}

function renderResolution(panel, data, inputs) {
    panel.style.display = 'block';
    panel.classList.remove('derived', 'error');
    panel.classList.add('derived');

    panel.innerHTML = `
        <div class="resolution-header">
            <span class="resolution-code">${escapeHtml(data.class_code)}</span>
        </div>
        ${renderInputPills(inputs)}
        ${renderPtTable(data.pressure_temperature)}
        ${renderDesignConditions(data.pressure_temperature)}
    `;
    wireDesignInputs(data.pressure_temperature);
}

function renderResolutionError(panel, message) {
    panel.style.display = 'block';
    panel.classList.remove('derived');
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

    const btn = document.getElementById('generateBtn');

    async function tryResolve() {
        const r = rating.value.trim();
        const m = material.value.trim();
        const c = ca.value.trim();
        if (!r || !m || !c) {
            panel.style.display = 'none';
            if (btn) btn.disabled = true;
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
                if (btn) btn.disabled = true;
                return;
            }
            renderResolution(panel, data, {
                rating:   r,
                material: m,
                ca:       c,
                service:  service ? service.value : '',
            });
            if (btn) btn.disabled = false;
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
