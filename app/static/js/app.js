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

// ---------------------------------------------------------------------------
// PMS report — Tab 1: Pressure-Temperature Rating
// All values are derived from formulas + Step 1 inputs + the P-T table.
// Three placeholder fields (Material Grade, Flange Ref, S₁/S₂) are left
// blank pending a data source decision.
// ---------------------------------------------------------------------------
const REPORT_CONST = {
    bargToPsig:      14.5038,        // unit conversion
    psigToBarg:      1 / 14.5038,
    millTolerance:   0.125,           // ASME B36.10M seamless
    operatingFactor: 0.8,             // operating ≈ 80% of design (rule of thumb)
    hydrotestFactor: 1.5,             // ASME B31.3 §345.4.2(a)
};

function bargToPsig(b) { return b * REPORT_CONST.bargToPsig; }
function cToF(c)       { return (c * 9 / 5) + 32; }

// Linear interpolation of the rated pressure at any temperature inside the curve.
function ratedPressureAtT(temps, pressures, targetT) {
    return interpolatePressure(temps, pressures, targetT);
}

// Pure-string parse — strip 'NACE' / 'LTCS' markers and parens to get a
// "clean" material name for display. Mirrors the resolver's normalisation.
function cleanMaterial(material) {
    return (material || '')
        .replace(/\bNACE\b/gi, '')
        .replace(/^LTCS/i, 'CS')
        .replace(/\(.*?\)/g, '')
        .trim()
        .replace(/\s+/g, ' ');
}

// Joint efficiency E lives in the dropdown labels themselves — parse it back
// out so we never need a separate constants table.
function jointEfficiencyFromLabel(jointType) {
    const labels = {
        'Seamless':       1.0,
        'EFW, 100% RT':   1.0,
        'ERW':            0.85,
        'EFW':            0.85,
    };
    return labels[jointType] ?? 1.0;
}

function kvRow(label, value, opts = {}) {
    const cls = opts.bold ? ' bold' : '';
    const tag = opts.tag ? `<span class="kv-tag ${opts.tag.cls}">${escapeHtml(opts.tag.text)}</span>` : '';
    const html = tag || `<span class="kv-value${cls}">${value}</span>`;
    return `<div class="kv-row"><span class="kv-label">${escapeHtml(label)}</span>${html}</div>`;
}

function fmt(n, digits = 1) {
    if (n == null || Number.isNaN(n)) return '—';
    return Number(n).toFixed(digits);
}

// ---- Population helpers ----

function populateBanner(state) {
    const banner = document.getElementById('rPmsBanner');
    if (!banner) return;

    const caLabel = /mm/i.test(state.ca) ? `${state.ca} CA` : state.ca;
    const services = (state.service || '').split(',').map(s => s.trim()).filter(Boolean);

    const pillsHtml = [
        `<span class="pms-banner-tag rating">${escapeHtml(state.rating)}</span>`,
        `<span class="pms-banner-tag material">${escapeHtml(cleanMaterial(state.material))}</span>`,
        `<span class="pms-banner-tag ca">${escapeHtml(caLabel)}</span>`,
        ...services.map(s => `<span class="pms-banner-tag service">${escapeHtml(s)}</span>`),
    ].join('');

    banner.innerHTML = `
        <div class="pms-banner-header">Generated PMS Code</div>
        <div class="pms-banner-code">${escapeHtml(state.classCode)}</div>
        <div class="pms-banner-details">${pillsHtml}</div>
        <div class="pms-banner-id">PMS-${escapeHtml(state.classCode)}</div>
        <div class="pms-banner-action">
            <button type="button" class="btn btn-regenerate" id="rRegenerateBtn">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/></svg>
                Regenerate with AI
            </button>
        </div>
    `;

    const regenBtn = document.getElementById('rRegenerateBtn');
    if (regenBtn) {
        regenBtn.addEventListener('click', () => {
            // Placeholder until ANTHROPIC_API_KEY + regen endpoint are wired.
            showToast('AI regeneration not wired yet — pending key + endpoint.', 'info', 4000);
        });
    }
}

function populatePmsInputs(state) {
    const ratingNum = state.rating.replace('#', '').trim();
    const ratingLabel = /^\d+$/.test(ratingNum) ? `${state.rating} (Class ${ratingNum})` : state.rating;
    const html = [
        kvRow('PMS Code',       `<span class="kv-value bold">${escapeHtml(state.classCode)}</span>`),
        kvRow('Pressure Rating', `<span class="kv-value">${escapeHtml(ratingLabel)}</span>`),
        kvRow('Material Type',   `<span class="kv-value">${escapeHtml(cleanMaterial(state.material))}</span>`),
        kvRow('Material Grade',  `<span class="kv-value" style="color:var(--text-muted);font-style:italic">— pending data source —</span>`),
    ].join('');
    document.getElementById('rPmsInputsList').innerHTML = html;
}

function populateServiceMaterial(state) {
    const isLowTemp = /^LTCS/i.test(state.material);
    const isNace    = /NACE/i.test(state.material);

    const html = [
        kvRow('Service',           `<span class="kv-value">${escapeHtml(state.service || '—')}</span>`),
        kvRow('Corrosion Allowance', `<span class="kv-value bold">${escapeHtml(state.ca)}</span>`),
        kvRow('Mill Tolerance',    `<span class="kv-value bold">${(REPORT_CONST.millTolerance * 100).toFixed(1)}%</span>`),
        kvRow('Low Temperature',   '', { tag: { cls: isLowTemp ? 'yes' : 'no', text: isLowTemp ? 'Yes' : 'No' } }),
        kvRow('NACE MR0175',       '', { tag: { cls: isNace ? 'yes' : 'no', text: isNace ? 'Yes' : 'No' } }),
    ].join('');
    document.getElementById('rServiceMaterialList').innerHTML = html;
}

function populateDerivedConditions(state, designPbarg, designTc, mdmtC) {
    const designPpsig  = bargToPsig(designPbarg);
    const operatingP   = designPbarg * REPORT_CONST.operatingFactor;
    const operatingPpsig = bargToPsig(operatingP);
    const operatingT   = designTc * REPORT_CONST.operatingFactor;

    // Hydrotest = max(rated pressure across the indexed envelope) × 1.5.
    // Falls back to design × 1.5 when no P-T curve is available.
    const maxRatedP = state.pt && state.pt.pressures_barg && state.pt.pressures_barg.length
        ? Math.max(...state.pt.pressures_barg)
        : designPbarg;
    const hydroP = maxRatedP * REPORT_CONST.hydrotestFactor;

    const pressureHtml = [
        `<div class="kv-row"><span class="kv-label">Design Pressure</span><span class="kv-value bold">${fmt(designPbarg, 1)} barg <span class="unit">(${fmt(designPpsig, 1)} psig)</span></span></div>`,
        `<div class="kv-row"><span class="kv-label">Hydrotest (1.5×&nbsp;max&nbsp;P)</span><span class="kv-value bold">${fmt(hydroP, 1)} barg <span class="unit">(${fmt(bargToPsig(hydroP), 1)} psig)</span></span></div>`,
        `<div class="kv-row warning"><span class="kv-label">Operating (est. 80% DP)</span><span class="kv-value">${fmt(operatingP, 1)} barg <span class="unit">(${fmt(operatingPpsig, 1)} psig)</span></span></div>`,
    ].join('');
    document.getElementById('rPressureCalcList').innerHTML = pressureHtml;

    const tempHtml = [
        `<div class="kv-row"><span class="kv-label">Design Temperature</span><span class="kv-value bold">${fmt(designTc, 0)}&deg;C <span class="unit">(${fmt(cToF(designTc), 1)}&deg;F)</span></span></div>`,
        `<div class="kv-row warning"><span class="kv-label">Operating (est. 80% DT)</span><span class="kv-value">${fmt(operatingT, 1)}&deg;C <span class="unit">(${fmt(cToF(operatingT), 1)}&deg;F)</span></span></div>`,
        `<div class="kv-row"><span class="kv-label">MDMT</span><span class="kv-value bold">${fmt(mdmtC, 0)}&deg;C <span class="unit">(${fmt(cToF(mdmtC), 1)}&deg;F)</span></span></div>`,
    ].join('');
    document.getElementById('rTempCalcList').innerHTML = tempHtml;
}

function populateStandardBar(state) {
    const bar = document.getElementById('rStandardBar');
    if (!state.pt || !state.pt.group) {
        bar.innerHTML = `<strong>Standard:</strong> ASME B16.5 / B31.3 &nbsp;|&nbsp; <strong>Class:</strong> ${escapeHtml(state.rating)} &nbsp;|&nbsp; <strong>Material:</strong> ${escapeHtml(cleanMaterial(state.material))}`;
        return;
    }
    bar.innerHTML = `<strong>Standard:</strong> ASME B16.5 &nbsp;|&nbsp; <strong>Group:</strong> ${escapeHtml(state.pt.group)} &nbsp;|&nbsp; <strong>Class:</strong> ${escapeHtml(state.rating)} &nbsp;|&nbsp; <strong>Material:</strong> ${escapeHtml(cleanMaterial(state.material))}`;
}

function populatePtTable(state, designTc) {
    const tbl = document.getElementById('rPtTable');
    if (!state.pt || !state.pt.temperatures_c || !state.pt.pressures_barg) {
        tbl.innerHTML = `<tbody><tr><td style="padding:24px;text-align:center;color:var(--text-muted)">No P-T data indexed for this combination.</td></tr></tbody>`;
        return;
    }
    const temps  = state.pt.temperatures_c;
    const press  = state.pt.pressures_barg;
    const labels = state.pt.temp_labels || temps.map(String);

    // Locate the column whose stored temperature equals the design T (exact
    // match only — interpolation is shown in the derived box, not the table).
    const highlightIdx = temps.findIndex(t => Number(t) === Number(designTc));

    const maxRatedP = Math.max(...press);
    const hydroP    = maxRatedP * REPORT_CONST.hydrotestFactor;

    const headCols = labels.map((l, i) => {
        const cls = (i === highlightIdx) ? 'col-highlight-header' : '';
        return `<th class="${cls}">${escapeHtml(String(l))}</th>`;
    }).join('');

    const pressRow = press.map((p, i) => {
        const cls = (i === highlightIdx) ? 'col-highlight' : '';
        return `<td class="${cls}">${fmt(p, 1)}</td>`;
    }).join('');

    const tempRow = temps.map((t, i) => {
        const cls = (i === highlightIdx) ? 'col-highlight' : '';
        return `<td class="${cls}">${escapeHtml(String(labels[i] ?? t))}</td>`;
    }).join('');

    tbl.innerHTML = `
        <thead>
            <tr>
                <th></th>
                ${headCols}
                <th rowspan="3" class="hydrotest-col">HYDROTEST PR. (BARG)</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td class="pt-row-head">Press., barg</td>
                ${pressRow}
            </tr>
            <tr>
                <td class="pt-row-head">Temp., °C</td>
                ${tempRow}
            </tr>
            <tr>
                <td colspan="${labels.length + 1}" class="hydrotest-cell">${fmt(hydroP, 1)}</td>
            </tr>
        </tbody>
    `;
    // Move the hydrotest cell up out of its placeholder row using a separate
    // structure: easier to rebuild the table with hydrotest as a single
    // right-side cell spanning two body rows.
    tbl.innerHTML = `
        <thead>
            <tr>
                <th></th>
                ${headCols}
                <th class="hydrotest-col">HYDROTEST PR. (BARG)</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td class="pt-row-head">Press., barg</td>
                ${pressRow}
                <td class="hydrotest-cell" rowspan="2">${fmt(hydroP, 1)}</td>
            </tr>
            <tr>
                <td class="pt-row-head">Temp., °C</td>
                ${tempRow}
            </tr>
        </tbody>
    `;
}

function populateAdequacy(state, designPbarg, designTc) {
    const box = document.getElementById('rAdequacyBox');
    if (!state.pt || !state.pt.temperatures_c) {
        box.className = 'adequacy-box';
        box.innerHTML = '<strong>P-T data not indexed</strong> — adequacy check unavailable.';
        return;
    }
    const ratedAtDesignT = ratedPressureAtT(state.pt.temperatures_c, state.pt.pressures_barg, designTc);
    const adequate = ratedAtDesignT >= designPbarg;
    box.className = `adequacy-box ${adequate ? 'pass' : 'fail'}`;
    box.innerHTML = adequate
        ? `&#10003; Class ${escapeHtml(state.rating)} is <strong>ADEQUATE</strong>: ${fmt(ratedAtDesignT, 1)} barg &ge; Design ${fmt(designPbarg, 1)} barg at ${fmt(designTc, 0)}&deg;C`
        : `&#10005; Class ${escapeHtml(state.rating)} is <strong>INADEQUATE</strong>: rating ${fmt(ratedAtDesignT, 1)} barg &lt; Design ${fmt(designPbarg, 1)} barg at ${fmt(designTc, 0)}&deg;C`;
}

// Two-way sync between barg and psig fields, plus °F display under temp inputs.
function wireReportInputs(state) {
    const pBarg = document.getElementById('rDesignPressure');
    const pPsig = document.getElementById('rDesignPressurePsig');
    const tC    = document.getElementById('rDesignTemperature');
    const tF    = document.getElementById('rTempFahrenheit');
    const mdmt  = document.getElementById('rMdmt');
    const mdmtF = document.getElementById('rMdmtFahrenheit');
    const joint = document.getElementById('rJointType');
    const jointRef = document.getElementById('rJointRef');

    let syncing = false;
    const refresh = () => {
        const dp = parseFloat(pBarg.value) || 0;
        const dt = parseFloat(tC.value) || 0;
        const md = parseFloat(mdmt.value) || 0;
        tF.textContent    = `= ${fmt(cToF(dt), 1)} °F`;
        mdmtF.textContent = `= ${fmt(cToF(md), 1)} °F`;
        const E = jointEfficiencyFromLabel(joint.value);
        jointRef.textContent = `ASME B31.3 Table A-1B  —  E = ${E.toFixed(2)}`;

        // Derived-conditions + adequacy + table highlighting recompute live.
        populateDerivedConditions(state, dp, dt, md);
        populatePtTable(state, dt);
        populateAdequacy(state, dp, dt);
    };

    pBarg.addEventListener('input', () => {
        if (syncing) return; syncing = true;
        pPsig.value = (parseFloat(pBarg.value) * REPORT_CONST.bargToPsig || 0).toFixed(1);
        syncing = false; refresh();
    });
    pPsig.addEventListener('input', () => {
        if (syncing) return; syncing = true;
        pBarg.value = (parseFloat(pPsig.value) * REPORT_CONST.psigToBarg || 0).toFixed(2);
        syncing = false; refresh();
    });
    [tC, mdmt, joint].forEach(el => el.addEventListener('input', refresh));

    refresh();
}

function wireReportTabs() {
    const tabs     = document.querySelectorAll('.report-tab');
    const contents = document.querySelectorAll('.report-tab-content');
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const target = tab.dataset.rtab;
            tabs.forEach(t => t.classList.toggle('active', t === tab));
            contents.forEach(c => c.classList.toggle('active', c.dataset.rtab === target));
            // Keep the user near the top of the tab so they don't land
            // mid-content when switching from a long scrolled tab.
            window.scrollTo({ top: document.getElementById('reportTabs').offsetTop - 80, behavior: 'smooth' });
        });
    });
}

function showReport(state) {
    document.getElementById('reportPanel').style.display = 'block';
    // Hide everything in Step 1 except the panel itself — keep navigation
    // stable but get the form, resolution card, and generate bar out of view.
    document.getElementById('pmsForm').parentElement.style.display = 'none';
    document.getElementById('classResolution').style.display = 'none';
    document.querySelector('.generate-bar').style.display = 'none';

    // Pre-fill the editable design conditions from Step 1.
    document.getElementById('rDesignPressure').value    = fmt(state.designP, 1);
    document.getElementById('rDesignPressurePsig').value = fmt(bargToPsig(state.designP), 1);
    document.getElementById('rDesignTemperature').value = fmt(state.designT, 0);

    populateBanner(state);
    populatePmsInputs(state);
    populateServiceMaterial(state);
    populateStandardBar(state);
    wireReportInputs(state);
    wireReportTabs();
    // Always land on the first tab when (re)opening the report.
    document.querySelectorAll('.report-tab').forEach((t, i) => t.classList.toggle('active', i === 0));
    document.querySelectorAll('.report-tab-content').forEach((c, i) => c.classList.toggle('active', i === 0));

    // Scroll to top of the report so the user lands at the title.
    document.getElementById('reportPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function hideReport() {
    document.getElementById('reportPanel').style.display = 'none';
    document.getElementById('pmsForm').parentElement.style.display = '';
    document.getElementById('classResolution').style.display = '';
    document.querySelector('.generate-bar').style.display = '';
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function wireForm() {
    const btn = document.getElementById('generateBtn');
    if (!btn) return;

    btn.addEventListener('click', () => {
        const rating   = document.getElementById('pipingClass').value.trim();
        const material = document.getElementById('material').value.trim();
        const ca       = document.getElementById('corrosionAllowance').value.trim();
        const service  = document.getElementById('service').value.trim();
        const designP  = parseFloat(document.getElementById('designPressure')?.value);
        const designT  = parseFloat(document.getElementById('designTemperature')?.value);

        const missing = { rating, material, ca, service };
        const missingKeys = Object.entries(missing).filter(([, v]) => !v).map(([k]) => k);
        if (missingKeys.length) {
            showToast(`Please fill: ${missingKeys.join(', ')}`, 'error');
            return;
        }
        if (Number.isNaN(designP) || Number.isNaN(designT)) {
            showToast('Design pressure and temperature must be set before generating.', 'error');
            return;
        }

        // The resolution card already cached its API response on dataset
        // for handoff into the report — pull pt + class_code from there.
        const cached = window._lastResolution || null;
        if (!cached) {
            showToast('Resolve the class before generating.', 'error');
            return;
        }

        const state = {
            rating, material, ca, service,
            classCode: cached.class_code,
            pt:        cached.pressure_temperature,
            designP, designT,
        };
        showReport(state);
    });

    const back = document.getElementById('backToFormBtn');
    if (back) back.addEventListener('click', hideReport);
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
            // Cache for the Generate-PMS handler — the report needs the
            // class code + P-T data resolved by this call.
            window._lastResolution = data;
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
