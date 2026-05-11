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
    npsDimensions:    () => fetch('/api/nps-dimensions'),
    pipeDimensions:   () => fetch('/api/pipe-dimensions'),
    pipeDimensionsSs: () => fetch('/api/pipe-dimensions-ss'),
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
        <div class="pms-banner-header">Resolved §5.5 PMS Code</div>
        <div class="pms-banner-code">${escapeHtml(state.classCode)}</div>
        <div class="pms-banner-details">${pillsHtml}</div>
        <div class="pms-banner-id">PMS-${escapeHtml(state.classCode)}</div>
    `;
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

// ---------------------------------------------------------------------------
// TAB 3 · Schedule & Wall Thickness — header section
//   Service strip + Design Parameters card + Fabrication & Code Factors card.
// Pure formulas and existing inputs only — three rows are blank placeholders
// (Material Spec, Allowable Stress S, Pipe Standard) pending data source.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Wall Thickness Calculation Table — body population.
//
// Same NPS list for every class (loaded once via /api/nps-dimensions and
// cached on window._npsDimensions). For now only the NPS and D columns
// carry data; the rest show '—' until the per-NPS calc is wired.
// ---------------------------------------------------------------------------
async function ensureNpsDimensions() {
    if (window._npsDimensions) return window._npsDimensions;
    try {
        const res = await API.npsDimensions();
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        window._npsDimensions = data;
        return data;
    } catch (e) {
        console.error('[nps-dimensions] failed:', e);
        showToast('Could not load NPS dimensions.', 'error', 5000);
        return null;
    }
}

// Full B36.10M Table 2-1, indexed by nps_decimal so the schedule rule
// can be applied in O(1) per row. Cached on window._b3610 after first fetch.
//
// We KEEP only rows where at least one of (schedule, identification) is
// defined. B36.10M also lists API 5L line-pipe-only intermediate wall
// thicknesses with both columns blank — those aren't standard ASME
// schedules and engineers don't normally pick them for B31.3 process
// piping. Including them caused the picker to land on un-named rows.
async function ensurePipeDimensions() {
    if (window._b3610) return window._b3610;
    try {
        const res = await API.pipeDimensions();
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const byNps = {};
        for (const row of data.rows || []) {
            if (row.schedule == null && row.identification == null) continue;
            const k = row.nps_decimal;
            if (!byNps[k]) byNps[k] = [];
            byNps[k].push(row);
        }
        for (const k of Object.keys(byNps)) {
            byNps[k].sort((a, b) => a.wt_mm - b.wt_mm);
        }
        window._b3610 = { source: data.source, by_nps: byNps };
        return window._b3610;
    } catch (e) {
        console.error('[pipe-dimensions] failed:', e);
        showToast('Could not load pipe dimensions (B36.10M).', 'error', 5000);
        return null;
    }
}

// B36.19M — stainless schedules (5S / 10S / 40S / 80S). Selected when
// the resolved class uses an austenitic stainless or 6 MO material.
async function ensurePipeDimensionsSs() {
    if (window._b3619) return window._b3619;
    try {
        const res = await API.pipeDimensionsSs();
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const byNps = {};
        for (const row of data.rows || []) {
            // SS rows always carry a schedule (no STD/XS aliases here).
            if (row.wt_mm == null) continue;  // skip the "…" placeholder rows
            const k = row.nps_decimal;
            if (!byNps[k]) byNps[k] = [];
            byNps[k].push(row);
        }
        for (const k of Object.keys(byNps)) {
            byNps[k].sort((a, b) => a.wt_mm - b.wt_mm);
        }
        window._b3619 = { source: data.source, by_nps: byNps };
        return window._b3619;
    } catch (e) {
        console.error('[pipe-dimensions-ss] failed:', e);
        showToast('Could not load stainless pipe dimensions (B36.19M).', 'error', 5000);
        return null;
    }
}

// Pick the right dimension table for the resolved material. Stainless /
// austenitic / 6 MO materials use B36.19M (S-suffix schedules); every-
// thing else uses B36.10M. Mirror of y_lookup's category detection.
function _materialUsesStainlessSchedules(material) {
    if (!material) return false;
    return /(?:^|\b)(SS\s*316|SS\s*304|TP\s*316|TP\s*304|6\s*MO|N08367)/i.test(material);
}

// Schedule pick — B36.10M for carbon-family materials, B36.19M for
// stainless / 6 MO. Returns the lightest row whose WT ≥ calcThk for
// the given NPS. Falls back to the heaviest row + status='NOT OK' when
// nothing qualifies, so the engineer sees the gap rather than '—'.
function pickSchedule(npsDecimal, calcThkMm, material) {
    const useSs = _materialUsesStainlessSchedules(material);
    const tableObj = useSs ? window._b3619 : window._b3610;
    const idx = tableObj && tableObj.by_nps;
    if (!idx) return null;
    const rows = idx[npsDecimal];
    if (!rows || !rows.length) return null;
    if (calcThkMm == null || Number.isNaN(calcThkMm)) return null;

    // Rows are sorted ascending by WT.
    let pick = rows.find(r => r.wt_mm >= calcThkMm) || null;
    let status = 'OK';
    if (!pick) {
        pick = rows[rows.length - 1];
        status = 'NOT OK';
    }

    // Display rule: numeric schedule if present, else identification
    // (STD / XS / XXS from B36.10M — B36.19M rows always have a schedule).
    const display = pick.schedule != null ? pick.schedule : (pick.identification || '—');

    return {
        sch_display: String(display),
        wt_mm:       pick.wt_mm,
        status,
        table:       useSs ? 'B36.19M' : 'B36.10M',
        row:         pick,
    };
}

// '3 mm' → 3, 'NIL' → 0, '1.5 mm' → 1.5
function parseCorrosionMm(ca) {
    if (!ca) return 0;
    if (/^\s*NIL\s*$/i.test(ca)) return 0;
    const m = String(ca).match(/(\d+(?:\.\d+)?)/);
    return m ? parseFloat(m[1]) : 0;
}

// B31.3 §304.1.2 Eq. 3a — pressure-thickness ratio per case.
// Returns null when any factor is missing (lets the row fall back to '—').
//
// All inputs are in psi / dimensionless: t/D = P / [2·(S·E·W + P·Y)]
function tDratio(P_psi, S_psi, E, Y, W) {
    if (P_psi == null || S_psi == null || E == null || Y == null || W == null) return null;
    const denom = 2 * (S_psi * E * W + P_psi * Y);
    return denom > 0 ? P_psi / denom : null;
}

// Per-NPS computation. Returns one row per NPS with t / D/6 / validity /
// tm / mill_tol / calc_thk filled in (or null when inputs are missing).
function computeWallThicknessRows(state, designPbarg, designTc) {
    const dims = window._npsDimensions;
    if (!dims || !dims.rows) return [];

    const cf          = (state && state.codeFactors) || {};
    const stressTable = cf.stress_table;
    const yCurve      = cf.y_curve;

    // Pressure values (psi). Case 1 = cold rated point; Case 2 = user's design point.
    const coldPbarg = state.pt && state.pt.cold_point ? state.pt.cold_point.pressure_barg : null;
    const coldTc    = state.pt && state.pt.temperatures_c && state.pt.temperatures_c.length
                      ? state.pt.temperatures_c[0] : null;

    const P1_psi = coldPbarg != null ? bargToPsig(coldPbarg) : null;
    const P2_psi = designPbarg != null ? bargToPsig(designPbarg) : null;

    // Stress values (psi) — interpolated from B31.3 Table A-1 via lookupStress.
    const sCold = (stressTable && coldTc != null) ? lookupStress(stressTable, coldTc) : null;
    const sHot  = (stressTable && designTc != null) ? lookupStress(stressTable, designTc) : null;
    const S1_psi = sCold ? sCold.stress_psi : null;
    const S2_psi = sHot  ? sHot.stress_psi  : null;

    // Code factors. E from joint type, Y from B31.3 Table 304.1.1, W=1 below 510°C.
    const joint = document.getElementById('rJointType')?.value || 'Seamless';
    const E     = jointEfficiencyFromLabel(joint);
    const yLook = lookupY(yCurve, designTc);
    const Y     = yLook ? yLook.y : null;
    const W     = (designTc != null && designTc <= 510) ? 1.0 : null;

    // Corrosion allowance + fixed mill tolerance.
    const C_mm    = parseCorrosionMm(state.ca);
    const millTol = 0.125;

    // Compute the two t/D ratios and take the worst case.
    const tD1 = tDratio(P1_psi, S1_psi, E, Y, W);
    const tD2 = tDratio(P2_psi, S2_psi, E, Y, W);
    const candidates = [tD1, tD2].filter(v => v != null && Number.isFinite(v));
    const tDmax = candidates.length ? Math.max(...candidates) : null;

    return dims.rows.map(r => {
        const D       = r.od_mm;
        const t_mm    = (tDmax != null) ? tDmax * D : null;
        const dOver6  = D / 6;
        const valid   = (t_mm != null) ? (t_mm < dOver6 ? 'OK' : 'ALERT') : null;
        const tm      = (t_mm != null) ? t_mm + C_mm : null;
        const calcThk = (tm != null) ? tm / (1 - millTol) : null;

        // Schedule pick from B36.10M (CS family) or B36.19M (stainless),
        // §9 rule: lightest WT ≥ Calc.Thk.
        const sched = (calcThk != null)
            ? pickSchedule(parseFloat(r.nps), calcThk, state.material)
            : null;
        const sel_thk_mm  = sched ? sched.wt_mm : null;
        const sch_display = sched ? sched.sch_display : null;
        const sch_status  = sched ? sched.status : null;

        // MAWP @ design T, per B31.3 inverted Eq. 3a:
        //   P_max = 2·S·E·W·t_eff / (D − 2·Y·t_eff)
        //   t_eff = SEL.THK·(1 − mill) − c   (worst-case actual wall after
        //                                      mill undertolerance + CA)
        let mawp_barg  = null;
        let margin_pct = null;
        if (sel_thk_mm != null && S2_psi != null && Number.isFinite(W)) {
            const t_eff_mm = sel_thk_mm * (1 - millTol) - C_mm;
            if (t_eff_mm > 0) {
                const t_eff_in = t_eff_mm / 25.4;
                const D_in     = D / 25.4;
                const denom    = D_in - 2 * Y * t_eff_in;
                if (denom > 0) {
                    const mawp_psi = (2 * S2_psi * E * W * t_eff_in) / denom;
                    mawp_barg = mawp_psi / 14.5038;
                    if (designPbarg && designPbarg > 0) {
                        margin_pct = ((mawp_barg - designPbarg) / designPbarg) * 100;
                    }
                }
            }
        }

        return {
            nps:      r.nps,
            od_mm:    D,
            t_mm,
            d_over_6: dOver6,
            validity: valid,
            tm_mm:    tm,
            mill_tol: millTol,
            calc_thk_mm: calcThk,
            sch_display,
            sel_thk_mm,
            sch_status,
            mawp_barg,
            margin_pct,
        };
    });
}

// ---------------------------------------------------------------------------
// Summary Statistics card — aggregates MAWP / margin / hydrotest across
// every NPS row in the Wall Thickness table. Same computation source so
// the numbers never disagree.
// ---------------------------------------------------------------------------
function renderSummaryStats(state, designPbarg, designTc) {
    const container = document.getElementById('rSummaryStats');
    if (!container) return;

    const rows = (state && designPbarg != null && designTc != null && !Number.isNaN(designPbarg) && !Number.isNaN(designTc))
        ? computeWallThicknessRows(state, designPbarg, designTc)
        : [];

    const mawps   = rows.map(r => r.mawp_barg).filter(v => v != null && Number.isFinite(v));
    const margins = rows.map(r => r.margin_pct).filter(v => v != null && Number.isFinite(v));

    // Hydrotest = max rated P × 1.5 per B31.3 §345.4.2(a)
    const maxRatedPbarg = state && state.pt && state.pt.pressures_barg && state.pt.pressures_barg.length
        ? Math.max(...state.pt.pressures_barg)
        : (designPbarg || null);
    const hydroBarg = maxRatedPbarg != null ? maxRatedPbarg * 1.5 : null;

    const minMawp = mawps.length   ? Math.min(...mawps)   : null;
    const maxMawp = mawps.length   ? Math.max(...mawps)   : null;
    const minMargin = margins.length ? Math.min(...margins) : null;

    const fmt = (v, unit, dp = 1) =>
        v == null || Number.isNaN(v)
            ? '<span class="kv-value" style="color:var(--text-muted)">—</span>'
            : `<span class="kv-value">${v.toFixed(dp)}${unit ? ' ' + unit : ''}</span>`;
    const fmtBold = (v, unit, dp = 1) =>
        v == null || Number.isNaN(v)
            ? '<span class="kv-value bold">—</span>'
            : `<span class="kv-value bold">${v.toFixed(dp)}${unit ? ' ' + unit : ''}</span>`;

    container.innerHTML = [
        `<div class="kv-row"><span class="kv-label">Min MAWP</span>${fmt(minMawp, 'barg')}</div>`,
        `<div class="kv-row"><span class="kv-label">Max MAWP</span>${fmt(maxMawp, 'barg')}</div>`,
        `<div class="kv-row"><span class="kv-label">Min Pressure Margin</span>${fmt(minMargin, '%', 1)}</div>`,
        `<div class="kv-row"><span class="kv-label">Hydrotest Pressure (1.5×P)</span>${fmtBold(hydroBarg, 'barg')}</div>`,
        `<div class="kv-row"><span class="kv-label">Total NPS Sizes</span><span class="kv-value">${rows.length}</span></div>`,
    ].join('');
}

// ---------------------------------------------------------------------------
// Tab 4 — Pipe & Fittings Material Assignment
//
// Two component tables (Small Bore / Large Bore) + a branch-chart card.
// Material specs come from code_factors.fitting_specs (per material family,
// industry-standard ASTM/ASME combinations). Schedule per bore is derived
// from the same Wall Thickness rows the Tab 3 table uses — picked from
// the dominant NPS in each bore range.
// ---------------------------------------------------------------------------

// Connection wording matches the project's Excel screenshots:
//   small bore → typ. seamless pipe with butt-weld (or socket weld) fittings
//   large bore → welded pipe acceptable, butt-weld throughout
// `material_family` from fitting_specs lets us tweak this for SS / DSS
// where fully welded is the norm even at small bore.
function _connectionWording(family, isLargeBore) {
    if (!family) return '—';
    if (isLargeBore) return 'Butt Weld (SCH to match pipe), Welded';
    // Small bore: copper / GRE / CPVC use sweat / threaded / solvent-weld
    if (/COPPER/i.test(family))   return 'Sweat / threaded fitting per B16.22';
    if (/GRE/i.test(family))      return 'Adhesive bonded / threaded per ISO 14692';
    if (/CPVC/i.test(family))     return 'Solvent weld per ASTM F 493';
    if (/TUBING/i.test(family))   return 'Compression / cone & thread (Swagelok)';
    return 'Butt Weld (SCH to match pipe), Seamless';
}

// Find the dominant schedule across an NPS range. Returns the most common
// schedule string in that range — engineers spec one schedule per bore-band
// rather than one per NPS, so "mode" is the right aggregate.
function _dominantScheduleInRange(rows, npsLo, npsHi) {
    const counts = {};
    for (const r of rows || []) {
        const nps = parseFloat(r.nps);
        if (Number.isNaN(nps) || nps < npsLo || nps > npsHi) continue;
        if (!r.sch_display) continue;
        counts[r.sch_display] = (counts[r.sch_display] || 0) + 1;
    }
    let best = null, bestN = 0;
    for (const [sch, n] of Object.entries(counts)) {
        if (n > bestN) { best = sch; bestN = n; }
    }
    return best;
}

// One row block in a component table. The standard column carries the
// reference standard for that fitting type — these stay constant across
// material families (B16.9 covers the geometry; the metallurgy comes from
// the spec column).
const _COMPONENT_ROWS = [
    { name: '90° LR Elbow',        spec_key: 'fittings',     sch_kind: 'pipe',      standard: 'ASME B 16.9' },
    { name: '45° Elbow',           spec_key: 'fittings',     sch_kind: 'pipe',      standard: 'ASME B 16.9' },
    { name: 'Equal Tee',           spec_key: 'fittings',     sch_kind: 'pipe',      standard: 'ASME B 16.9' },
    { name: 'Reducing Tee',        spec_key: 'fittings',     sch_kind: 'pipe',      standard: 'ASME B 16.9' },
    { name: 'Concentric Reducer',  spec_key: 'fittings',     sch_kind: 'pipe',      standard: 'ASME B 16.9' },
    { name: 'Eccentric Reducer',   spec_key: 'fittings',     sch_kind: 'pipe',      standard: 'ASME B 16.9' },
    { name: 'Pipe Cap',            spec_key: 'fittings',     sch_kind: 'b16.9',     standard: 'ASME B 16.9' },
    { name: 'Plug',                spec_key: 'fittings',     sch_kind: '—',         standard: 'Hex Head Plug, ASME B 16.11' },
    { name: 'Weldolet',            spec_key: 'branch_outlet',sch_kind: '—',         standard: 'MSS SP-97' },
];

function _formatSchedule(kind, schDisp) {
    if (kind === '—' || !schDisp) return '—';
    if (kind === 'b16.9')         return 'ASME B 16.9';
    if (schDisp.startsWith('Sch')) return schDisp;
    // For pipe-matching fittings the convention is "Sch <num> / XS" if the
    // schedule has both numeric + identification representations; when it's
    // just identification we surface that. Single-token fallback otherwise.
    return `Sch ${schDisp}`;
}

function _fittingSchedule(kind, pipeSchDisp) {
    if (kind === '—' || !pipeSchDisp) return '—';
    if (kind === 'b16.9') return 'ASME B 16.9';
    // Component fittings track the pipe schedule. We surface both the
    // numeric and identification forms when both exist (e.g. "Sch 80 / XS").
    // Without the underlying row's identification handy here, just prefix.
    return `Sch ${pipeSchDisp}`;
}

function renderPipeFittingsTab(state, designPbarg, designTc) {
    const cf    = (state && state.codeFactors) || {};
    const specs = cf.fitting_specs;
    const small = document.getElementById('rSmallBoreBody');
    const large = document.getElementById('rLargeBoreBody');
    const smallInfo = document.getElementById('rSmallBoreInfo');
    const largeInfo = document.getElementById('rLargeBoreInfo');
    const branch    = document.getElementById('rBranchChart');
    if (!small || !large) return;

    if (!specs) {
        const empty = '<div class="bore-info-bar-empty">No fitting spec mapped for this material — pending project data source.</div>';
        smallInfo && (smallInfo.outerHTML = empty.replace('id-x', 'rSmallBoreInfo'));
        largeInfo && (largeInfo.outerHTML = empty);
        small.innerHTML = '';
        large.innerHTML = '';
        if (branch) branch.textContent = '—';
        return;
    }

    // Dynamic schedule selection from the Wall Thickness table rows.
    const rows = (state && designPbarg != null && designTc != null && !Number.isNaN(designPbarg) && !Number.isNaN(designTc))
        ? computeWallThicknessRows(state, designPbarg, designTc)
        : [];
    const smallSch = _dominantScheduleInRange(rows, 0, 2)   || '—';
    const largeSch = _dominantScheduleInRange(rows, 2.5, 80) || '—';

    const fmtSch = (s) => /^[A-Z]{2,}$/.test(s) ? s : `SCH ${s}`;

    // Info bars
    if (smallInfo) {
        smallInfo.innerHTML = `<strong>Connection:</strong> ${escapeHtml(_connectionWording(specs.family, false))}
            &nbsp;&nbsp;|&nbsp;&nbsp; <strong>Schedule:</strong> ${escapeHtml(fmtSch(smallSch))}`;
    }
    if (largeInfo) {
        largeInfo.innerHTML = `<strong>Connection:</strong> ${escapeHtml(_connectionWording(specs.family, true))}
            &nbsp;&nbsp;|&nbsp;&nbsp; <strong>Schedule:</strong> ${escapeHtml(fmtSch(largeSch))}`;
    }

    // Build one tbody for each bore.
    function buildBody(boreSch) {
        const rows = [];
        // Pipe row first
        rows.push(`
            <tr>
                <td class="comp-name">Pipe</td>
                <td>${escapeHtml(specs.pipe || '—')}</td>
                <td>${escapeHtml(fmtSch(boreSch))}</td>
                <td>ASTM</td>
            </tr>
        `);
        for (const c of _COMPONENT_ROWS) {
            const mat = specs[c.spec_key] || '—';
            const sch = _fittingSchedule(c.sch_kind, boreSch);
            rows.push(`
                <tr>
                    <td class="comp-name">${escapeHtml(c.name)}</td>
                    <td>${escapeHtml(mat)}</td>
                    <td>${escapeHtml(sch)}</td>
                    <td>${escapeHtml(c.standard)}</td>
                </tr>
            `);
        }
        return rows.join('');
    }

    small.innerHTML = buildBody(smallSch);
    large.innerHTML = buildBody(largeSch);

    // Branch Connection Chart — render the full Appendix-1 triangular
    // matrix per the material family (Chart 1/2/3/4 picked server-side).
    if (branch) {
        renderBranchChart(branch, (state.codeFactors || {}).branch_chart);
    }
}


function _branchCellClass(code) {
    // Color-code cells by fitting type for visual scanning.
    if (code === 'T')  return 'bc-tee';
    if (code === 'RT') return 'bc-rtee';
    if (code === 'W')  return 'bc-weldolet';
    if (code === 'S')  return 'bc-sockolet';
    if (code === 'H')  return 'bc-threadolet';
    if (code === '-')  return 'bc-na';
    return '';
}


function _fmtNps(n) {
    // 0.5 → 1/2", 0.75 → 3/4", 1.5 → 1-1/2", integers → 1", 2", 24"
    if (n === 0.5) return '½"';
    if (n === 0.75) return '¾"';
    if (n === 1.5) return '1½"';
    return `${n}"`;
}


function renderBranchChart(host, chart) {
    if (!chart || !chart.matrix || !chart.nps_axis) {
        host.innerHTML = '<div class="bore-info-bar-empty">No branch chart mapped for this material.</div>';
        return;
    }
    const axis = chart.nps_axis;
    const matrix = chart.matrix;

    // Header row — branch NPS along the top
    const headerCells = axis.map(n => `<th class="bc-axis-col">${_fmtNps(n)}</th>`).join('');

    // Body — row for each run pipe NPS; only fill cells up to header_idx (triangular)
    const bodyRows = matrix.map((row, ridx) => {
        const runNps = axis[ridx];
        const cells = axis.map((_, cidx) => {
            if (cidx < row.length) {
                const code = row[cidx];
                return `<td class="bc-cell ${_branchCellClass(code)}">${escapeHtml(code)}</td>`;
            }
            return '<td class="bc-cell bc-empty"></td>';
        }).join('');
        return `<tr><th class="bc-axis-row">${_fmtNps(runNps)}</th>${cells}</tr>`;
    }).join('');

    // Legend
    const legendItems = Object.entries(chart.legend || {})
        .map(([code, label]) =>
            `<span class="bc-legend-item"><span class="bc-legend-code ${_branchCellClass(code)}">${escapeHtml(code)}</span>${escapeHtml(label)}</span>`)
        .join('');

    host.innerHTML = `
        <div class="bc-title-bar">
            <div class="bc-title">${escapeHtml(chart.title || '')}</div>
            ${chart.subtitle ? `<div class="bc-subtitle">${escapeHtml(chart.subtitle)}</div>` : ''}
            ${chart.resolved_family ? `<div class="bc-family">For material family: <strong>${escapeHtml(chart.resolved_family)}</strong></div>` : ''}
        </div>
        <div class="bc-scroll">
            <table class="bc-table">
                <thead>
                    <tr>
                        <th class="bc-corner">RUN ↓ / BRANCH →</th>
                        ${headerCells}
                    </tr>
                </thead>
                <tbody>${bodyRows}</tbody>
            </table>
        </div>
        <div class="bc-legend">${legendItems}</div>
    `;
}

// ---------------------------------------------------------------------------
// Tab 5 — Component cards (Flange, Bolts/Nuts/Gaskets, Valves, Spectacle)
//
// All four cards are populated from state.codeFactors.flange_extras and
// fitting_specs. Re-rendered on every refresh so any change to material /
// rating / NACE flag flows through.
// ---------------------------------------------------------------------------

function _kvRow(label, value, opts) {
    opts = opts || {};
    const bold = opts.bold ? ' bold' : '';
    const muted = opts.muted ? ' style="color:var(--text-muted);font-style:italic"' : '';
    return `<div class="kv-row"><span class="kv-label">${escapeHtml(label)}</span>` +
           `<span class="kv-value${bold}"${muted}>${value}</span></div>`;
}

function renderFlangeCard(state) {
    const card = document.getElementById('rFlangeCard');
    if (!card) return;
    const cf   = state.codeFactors || {};
    const fs   = cf.fitting_specs || {};
    const fx   = cf.flange_extras || {};
    const face = fx.face || {};
    const type_ = fx.type || {};

    const rows = [];
    rows.push(_kvRow('MOC', escapeHtml(fs.flange || '—'), { bold: true }));
    rows.push(_kvRow('FACE',
        `${escapeHtml(state.rating || '—')}, <strong>${escapeHtml(face.code || '—')}</strong>` +
        ` <span class="unit">(${escapeHtml(face.label || '')})</span>`));
    rows.push(_kvRow('Type', escapeHtml(type_.type || '—')));
    rows.push(`<div class="kv-row top"><span class="kv-label">Compact Flange</span>` +
        `<span class="kv-value" style="text-align:right;max-width:65%;font-size:0.82rem">` +
        `${escapeHtml(type_.compact || '—')}</span></div>`);
    rows.push(`<div class="kv-row top"><span class="kv-label">Hub Connector</span>` +
        `<span class="kv-value" style="text-align:right;max-width:65%;font-size:0.82rem">` +
        `${escapeHtml(type_.hub || '—')}</span></div>`);

    card.innerHTML = rows.join('');
}

function renderBoltsCard(state) {
    const card = document.getElementById('rBoltsCard');
    if (!card) return;
    const fx = (state.codeFactors || {}).flange_extras || {};
    const b  = fx.bolting || {};
    const g  = fx.gasket  || {};
    const rows = [
        `<div class="kv-row top"><span class="kv-label">Stud Bolts</span>` +
            `<span class="kv-value" style="text-align:right;max-width:65%;font-size:0.82rem">` +
            `${escapeHtml(b.stud || '—')}</span></div>`,
        `<div class="kv-row top"><span class="kv-label">Hex Nuts</span>` +
            `<span class="kv-value" style="text-align:right;max-width:65%;font-size:0.82rem">` +
            `${escapeHtml(b.hex_nut || '—')}</span></div>`,
        `<div class="kv-row top"><span class="kv-label">Gasket</span>` +
            `<span class="kv-value" style="text-align:right;max-width:65%;font-size:0.82rem">` +
            `${escapeHtml(g.spec || '—')}</span></div>`,
    ];
    card.innerHTML = rows.join('');
}

function renderSpectacleCard(state) {
    const card = document.getElementById('rSpectacleCard');
    if (!card) return;
    const fx = (state.codeFactors || {}).flange_extras || {};
    const s  = fx.spectacle || {};
    card.innerHTML = [
        _kvRow('MOC', escapeHtml(s.moc || '—'), { bold: true }),
        _kvRow('Standard (Small)', escapeHtml(s.small_bore || '—')),
        _kvRow('Standard (Large)', escapeHtml(s.large_bore || '—')),
    ].join('');
}

function renderValvesCard(state) {
    const card = document.getElementById('rValvesCard');
    if (!card) return;
    const fx = (state.codeFactors || {}).flange_extras || {};
    const v  = fx.valves || {};

    // Project valve codes per §5.5 nomenclature
    // [TYPE 2ch][SUBTYPE 1ch][SEAT 1ch][class base][FACE 1ch]
    // Code shown prominently; description as secondary line for engineering review.
    const codeRow = (label, item) => {
        if (!item) return '';
        const code = (item && item.code) || '—';
        const desc = (item && item.desc) || '';
        return `<div class="kv-row top"><span class="kv-label">${escapeHtml(label)}</span>` +
               `<span class="kv-value valve-cell">` +
               `<span class="valve-code">${escapeHtml(code)}</span>` +
               (desc ? `<span class="valve-desc">${escapeHtml(desc)}</span>` : '') +
               `</span></div>`;
    };

    card.innerHTML = [
        _kvRow('Rating',       escapeHtml(v.rating || '—'), { bold: true }),
        _kvRow('Body MOC',     escapeHtml(v.body || '—'),   { bold: true }),
        codeRow('Ball',        v.ball),
        codeRow('Gate',        v.gate),
        codeRow('Globe',       v.globe),
        codeRow('Check',       v.check),
        codeRow('Butterfly',   v.butterfly),
        codeRow('DBB',         v.dbb),
        codeRow('DBB (Inst.)', v.dbb_inst),
    ].join('');
}

function renderTab5Components(state) {
    renderFlangeCard(state);
    renderBoltsCard(state);
    renderSpectacleCard(state);
    renderValvesCard(state);
}


// ---------------------------------------------------------------------------
// Tab 6 — Datasheet (Excel-style single-page view)
//
// Mirrors the layout of the project's PMS Excel deliverable (PMS-F.pdf):
// header bar, P-T envelope, pipe data split into small/large bore,
// fittings/flange/spectacle/bolts/valves/notes. All data sourced from the
// already-resolved `state` — no extra API calls.
// ---------------------------------------------------------------------------

const DS_NPS_AXIS = [0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32];

function _dsFmtNps(n) {
    if (n === 0.5)  return '0.5';
    if (n === 0.75) return '0.75';
    if (n === 1.5)  return '1.5';
    return String(n);
}

function _dsRevisionTag() { return 'A0'; }
function _dsSheetNo(state) {
    // Sheet number is project-managed; surface the class code as a stand-in.
    return state.classCode || '—';
}

function _dsDesignCode(state) {
    const isNace = /NACE/i.test(state.material || '');
    return isNace
        ? 'ASME B 31.3, NACE-MR-01-75 / ISO-15156-1/2/3'
        : 'ASME B 31.3';
}

function _dsMillTol() { return '12.5%'; }

function _dsPipeType(material) {
    const u = (material || '').toUpperCase();
    if (u.includes('SS') || u.includes('TP3') || u.includes('DSS') || u.includes('SDSS')) {
        return { sml: 'Seamless', lrg: 'Welded, 100% RT' };
    }
    return { sml: 'Seamless', lrg: 'LSAW, 100% RT' };
}

function _dsPipeMoc(specs, material, isLarge) {
    if (!specs) return '—';
    // 150# CS LSAW project convention pairs A106 Gr B (seamless) with
    // API 5L Gr B (large-bore welded). For other families, the pipe field
    // already lists the combined spec.
    const u = (material || '').toUpperCase();
    if (isLarge && /\bCS\b/.test(u) && !u.includes('GALV')) {
        return 'API 5L Gr. B';
    }
    return specs.pipe || '—';
}

function _dsPickSch(rows, lo, hi) {
    const counts = {};
    for (const r of rows) {
        const npsNum = parseFloat(r.nps);
        if (!r.sch || Number.isNaN(npsNum)) continue;
        if (npsNum >= lo && npsNum <= hi) {
            counts[r.sch] = (counts[r.sch] || 0) + 1;
        }
    }
    let best = null, bestCount = 0;
    for (const [s, c] of Object.entries(counts)) {
        if (c > bestCount) { bestCount = c; best = s; }
    }
    return best;
}

function _dsRowsByNps(rows) {
    const map = {};
    for (const r of rows) {
        const k = parseFloat(r.nps);
        if (!Number.isNaN(k)) map[k] = r;
    }
    return map;
}

function _dsFmt(v, dp = 1) {
    if (v == null || Number.isNaN(v)) return '—';
    return Number(v).toFixed(dp);
}

function renderDatasheetTab(state, designPbarg, designTc) {
    const host = document.getElementById('rDatasheet');
    if (!host) return;
    const cf = state.codeFactors || {};
    const fs = cf.fitting_specs || {};
    const fx = cf.flange_extras || {};
    const v  = fx.valves || {};
    const pt = state.pt || {};

    // P-T envelope rows for header table
    const ptTemps = pt.temperatures_c || [];
    const ptPress = pt.pressures_barg || [];
    const ptLabels = pt.temp_labels || ptTemps.map(String);
    const hydroBarg = ptPress.length ? Math.max(...ptPress) * 1.5 : (designPbarg || 0) * 1.5;

    // Pipe data — use the same WT table the Schedule tab computes
    const wtRows = computeWallThicknessRows(state, designPbarg, designTc) || [];
    const rowsByNps = _dsRowsByNps(wtRows);

    // Bore schedule selections
    const smallSch = _dsPickSch(wtRows, 0, 2) || '—';
    const largeSch = _dsPickSch(wtRows, 2.5, 80) || '—';

    const pipeType = _dsPipeType(state.material);
    const pipeMocSm = _dsPipeMoc(fs, state.material, false);
    const pipeMocLg = _dsPipeMoc(fs, state.material, true);

    // ── Pipe Data table cells (size / OD / Sch / WT for each NPS) ──
    const npsCellsRow = DS_NPS_AXIS.map(n => `<td>${_dsFmtNps(n)}</td>`).join('');
    const odCellsRow  = DS_NPS_AXIS.map(n => {
        const r = rowsByNps[n];
        return `<td>${r ? _dsFmt(r.od_mm, 1) : '—'}</td>`;
    }).join('');
    const schCellsRow = DS_NPS_AXIS.map(n => {
        const r = rowsByNps[n];
        return `<td>${r && r.sch ? escapeHtml(r.sch) : '—'}</td>`;
    }).join('');
    const wtCellsRow  = DS_NPS_AXIS.map(n => {
        const r = rowsByNps[n];
        return `<td>${r && r.sel_thk_mm != null ? _dsFmt(r.sel_thk_mm, 2) : '—'}</td>`;
    }).join('');

    // Split TYPE / MOC / Ends across small + large bore columns
    // Small bore = NPS ≤ 2 (cols 1–5 in our 21-NPS axis), Large = NPS ≥ 2.5 (cols 6–21)
    const smallCols = 5;
    const largeCols = DS_NPS_AXIS.length - smallCols;
    const typeRow = `
        <td colspan="${smallCols}">${escapeHtml(pipeType.sml)}</td>
        <td colspan="${largeCols}">${escapeHtml(pipeType.lrg)}</td>`;
    const mocRow = `
        <td colspan="${smallCols}">${escapeHtml(pipeMocSm)}</td>
        <td colspan="${largeCols}">${escapeHtml(pipeMocLg)}</td>`;
    const endsRow = `
        <td colspan="${smallCols}">BE</td>
        <td colspan="${largeCols}">BE</td>`;

    // P-T rating header (3 rows: title, press, temp + hydrotest column)
    const ptHeaderCells = ptTemps.map(() => '').join('');
    const pressCells = ptPress.map(p => `<td>${_dsFmt(p, 1)}</td>`).join('');
    const tempCells  = ptLabels.map(t => `<td>${escapeHtml(t)}</td>`).join('');

    // Valves block
    const vCode = k => (v[k] || {}).code || '—';

    // Fittings spec rows
    const fittingsMoc = fs.fittings || '—';
    const flangeMoc   = fs.flange   || '—';
    const branchMoc   = fs.branch_outlet || '—';

    // Bolts
    const stud = (fx.bolting || {}).stud || '—';
    const nut  = (fx.bolting || {}).hex_nut || '—';
    const gasket = (fx.gasket || {}).spec || '—';

    // Spectacle
    const sp = fx.spectacle || {};

    // Flange
    const flangeFace = (fx.face || {}).label || '—';
    const flangeType = (fx.type || {}).type || '—';
    const ratingNum  = state.rating || '—';
    const faceCode   = (fx.face || {}).code || '';
    const faceFull   = `${ratingNum} ${faceCode}, Serrated Finish`;

    const cleanMat = (typeof cleanMaterial === 'function') ? cleanMaterial(state.material) : state.material;

    host.innerHTML = `
        <div class="ds-sheet">
            <!-- ── Header ── -->
            <table class="ds-table ds-header-tbl">
                <tr>
                    <td class="ds-header-title" colspan="5">PIPING MATERIAL SPECIFICATION</td>
                    <td class="ds-rev-lbl">Rev :</td>
                    <td class="ds-rev-val">${escapeHtml(_dsRevisionTag())}</td>
                </tr>
                <tr class="ds-header-row">
                    <td class="ds-header-cell"></td>
                    <td class="ds-header-cell">Piping Class</td>
                    <td class="ds-header-cell">Material</td>
                    <td class="ds-header-cell">C.A</td>
                    <td class="ds-header-cell">Mill Tol</td>
                    <td class="ds-header-cell" colspan="2">Sheet No.</td>
                </tr>
                <tr>
                    <td class="ds-class-badge"></td>
                    <td class="ds-id-value"><strong>${escapeHtml(state.classCode || '—')}</strong></td>
                    <td class="ds-id-value">${escapeHtml(state.rating || '—')}</td>
                    <td class="ds-id-value">${escapeHtml(cleanMat || '—')}</td>
                    <td class="ds-id-value">${escapeHtml(state.ca || '—')}</td>
                    <td class="ds-id-value">${escapeHtml(_dsMillTol())}</td>
                    <td class="ds-id-value">${escapeHtml(_dsSheetNo(state))}</td>
                </tr>
                <tr>
                    <td class="ds-label">Design Code:</td>
                    <td colspan="6" class="ds-value">${escapeHtml(_dsDesignCode(state))}</td>
                </tr>
                <tr>
                    <td class="ds-label">Service:</td>
                    <td colspan="6" class="ds-value">${escapeHtml(state.service || '—')}</td>
                </tr>
                <tr>
                    <td class="ds-label">Branch Chart:</td>
                    <td colspan="6" class="ds-value">Ref. APPENDIX-1, ${escapeHtml(((cf.branch_chart || {}).title || 'Chart 1').replace(/^CHART[-\s]*/i, 'Chart '))}</td>
                </tr>
            </table>

            <!-- ── P-T Rating ── -->
            <table class="ds-table">
                <tr class="ds-section-row">
                    <td colspan="${ptTemps.length + 2}">Pressure-Temperature Rating</td>
                </tr>
                <tr>
                    <td class="ds-label">Press., barg</td>
                    ${pressCells}
                    <td class="ds-merge-r" rowspan="2">Hydrotest Pr. (barg)</td>
                </tr>
                <tr>
                    <td class="ds-label">Temp., °C</td>
                    ${tempCells}
                </tr>
                <tr>
                    <td class="ds-label" colspan="${ptTemps.length + 1}"></td>
                    <td class="ds-id-value"><strong>${_dsFmt(hydroBarg, 1)}</strong></td>
                </tr>
            </table>

            <!-- ── Pipe Data ── -->
            <table class="ds-table ds-pipe-tbl">
                <tr class="ds-section-row">
                    <td colspan="${DS_NPS_AXIS.length + 1}">Pipe Data</td>
                </tr>
                <tr>
                    <td class="ds-label">Code</td>
                    <td colspan="${DS_NPS_AXIS.length}" class="ds-value">ASME B 36.10M</td>
                </tr>
                <tr>
                    <td class="ds-label">Size (in)</td>${npsCellsRow}
                </tr>
                <tr>
                    <td class="ds-label">O.D. mm</td>${odCellsRow}
                </tr>
                <tr>
                    <td class="ds-label">Sch.</td>${schCellsRow}
                </tr>
                <tr>
                    <td class="ds-label">WT. mm</td>${wtCellsRow}
                </tr>
                <tr>
                    <td class="ds-label">TYPE</td>${typeRow}
                </tr>
                <tr>
                    <td class="ds-label">MOC</td>${mocRow}
                </tr>
                <tr>
                    <td class="ds-label">Ends</td>${endsRow}
                </tr>
            </table>

            <!-- ── Fittings Data ── -->
            <table class="ds-table">
                <tr class="ds-section-row"><td colspan="3">Fittings Data</td></tr>
                <tr>
                    <td class="ds-label">TYPE</td>
                    <td class="ds-value">Butt Weld (SCH to match pipe), Seamless</td>
                    <td class="ds-value">Butt Weld (SCH to match pipe), Welded</td>
                </tr>
                <tr>
                    <td class="ds-label">MOC</td>
                    <td colspan="2" class="ds-value"><strong>${escapeHtml(fittingsMoc)}</strong></td>
                </tr>
                <tr><td class="ds-label">Elbow</td><td colspan="2" class="ds-value">ASME B 16.9</td></tr>
                <tr><td class="ds-label">Tee</td><td colspan="2" class="ds-value">ASME B 16.9</td></tr>
                <tr><td class="ds-label">Red.</td><td colspan="2" class="ds-value">ASME B 16.9</td></tr>
                <tr><td class="ds-label">Cap</td><td colspan="2" class="ds-value">ASME B 16.9</td></tr>
                <tr><td class="ds-label">Plug</td><td colspan="2" class="ds-value">Hex Head Plug, ASME B 16.11</td></tr>
                <tr><td class="ds-label">Weldolet</td><td colspan="2" class="ds-value">${escapeHtml(branchMoc)}</td></tr>
            </table>

            <!-- ── Flange ── -->
            <table class="ds-table">
                <tr class="ds-section-row"><td colspan="2">Flange</td></tr>
                <tr><td class="ds-label">MOC</td><td class="ds-value"><strong>${escapeHtml(flangeMoc)}</strong></td></tr>
                <tr><td class="ds-label">FACE</td><td class="ds-value">${escapeHtml(faceFull)}</td></tr>
                <tr><td class="ds-label">TYPE</td><td class="ds-value">${escapeHtml(flangeType)}</td></tr>
            </table>

            <!-- ── Spectacle Blind / Spacer Blinds ── -->
            <table class="ds-table">
                <tr class="ds-section-row"><td colspan="3">Spectacle Blind/Spacer Blinds</td></tr>
                <tr><td class="ds-label">MOC</td><td colspan="2" class="ds-value"><strong>${escapeHtml(sp.moc || flangeMoc)}</strong></td></tr>
                <tr>
                    <td class="ds-label">Spectacle</td>
                    <td class="ds-value">${escapeHtml(sp.small_bore || '—')}</td>
                    <td class="ds-value">${escapeHtml(sp.large_bore || '—')}</td>
                </tr>
            </table>

            <!-- ── Bolts / Nuts / Gaskets ── -->
            <table class="ds-table">
                <tr class="ds-section-row"><td colspan="2">Bolts/ Nuts/ Gaskets</td></tr>
                <tr><td class="ds-label">Stud Bolts</td><td class="ds-value">${escapeHtml(stud)}</td></tr>
                <tr><td class="ds-label">Hex Nuts</td><td class="ds-value">${escapeHtml(nut)}</td></tr>
                <tr><td class="ds-label">Gasket</td><td class="ds-value">${escapeHtml(gasket)}</td></tr>
            </table>

            <!-- ── Valves ── -->
            <table class="ds-table">
                <tr class="ds-section-row"><td colspan="2">Valves</td></tr>
                <tr><td class="ds-label">Rating</td><td class="ds-value">${escapeHtml(v.rating || '—')}</td></tr>
                <tr><td class="ds-label">Ball</td><td class="ds-value ds-code">${escapeHtml(vCode('ball'))}</td></tr>
                <tr><td class="ds-label">Gate</td><td class="ds-value ds-code">${escapeHtml(vCode('gate'))}</td></tr>
                <tr><td class="ds-label">Globe</td><td class="ds-value ds-code">${escapeHtml(vCode('globe'))}</td></tr>
                <tr><td class="ds-label">Check</td><td class="ds-value ds-code">${escapeHtml(vCode('check'))}</td></tr>
                ${v.butterfly ? `<tr><td class="ds-label">Butterfly</td><td class="ds-value ds-code">${escapeHtml(vCode('butterfly'))}</td></tr>` : ''}
                ${v.dbb ? `<tr><td class="ds-label">DBB</td><td class="ds-value ds-code">${escapeHtml(vCode('dbb'))}</td></tr>` : ''}
                ${v.dbb_inst ? `<tr><td class="ds-label">DBB (Inst.)</td><td class="ds-value ds-code">${escapeHtml(vCode('dbb_inst'))}</td></tr>` : ''}
            </table>

            <!-- ── Notes ── -->
            <table class="ds-table">
                <tr class="ds-section-row"><td colspan="2">NOTES</td></tr>
                <tr><td class="ds-note-num">1</td><td class="ds-value">PMS to be read in conjunction with Project Piping Design Basis, and Valve Material Specification.</td></tr>
                <tr><td class="ds-note-num">2</td><td class="ds-value">Weld Joint Factor for welded pipe shall be as per ASME B 31.3.</td></tr>
                <tr><td class="ds-note-num">3</td><td class="ds-value">Welded fittings shall be 100% radiographed.</td></tr>
                <tr><td class="ds-note-num">4</td><td class="ds-value">Spectacle blinds and spacer sizes and rating that are not available in ASME B 16.48 shall be as per manuf. standard. Design shall be submitted to Company for review and approval.</td></tr>
                <tr><td class="ds-note-num">5</td><td class="ds-value">Maximum temperature limit for all Soft Seat Ball Valve shall be 250°C.</td></tr>
                <tr><td class="ds-note-num">6</td><td class="ds-value">Wafer check valve to be avoided, unless the available space constraint does not allow normal check valve.</td></tr>
                <tr><td class="ds-note-num">7</td><td class="ds-value">Wafer type Butterfly Valve may be used only in water service and shall not be used in hydrocarbon service.</td></tr>
                <tr><td class="ds-note-num">8</td><td class="ds-value">Two jackscrew, 180 degree apart shall be provided in one of the flanges for all orifice flange and specified spectacle blind assemblies.</td></tr>
            </table>
        </div>
    `;
}


function renderTagLegend(state) {
    const container = document.getElementById('rTagLegend');
    if (!container) return;
    // Tags that can appear on per-NPS rows. Right now Eq. 3a always
    // governs every row (we don't distinguish pressure-thickness from
    // PMS-minimum or hand-calculated), so the legend shows one entry.
    // More tags (NACE / LTCS / GOVERNS-PMS-MIN / CUSTOM) get added here
    // when the table starts tagging rows.
    const items = [
        { cls: 'pressure', label: 'Pressure', desc: 'ASME B31.3 Eq. 3a governs' },
    ];
    container.innerHTML = items.map(it => `
        <div class="legend-row">
            <span class="pipe-tag pipe-tag-${it.cls}">${escapeHtml(it.label)}</span>
            <span class="legend-desc">${escapeHtml(it.desc)}</span>
        </div>
    `).join('');
}

function populateWallThicknessTable(state, designPbarg, designTc) {
    const tbody = document.querySelector('#rWallThicknessTable tbody');
    if (!tbody) return;
    const dims = window._npsDimensions;
    if (!dims || !dims.rows) {
        tbody.innerHTML = '<tr><td colspan="11" style="padding:20px;color:var(--text-muted);">No NPS data loaded.</td></tr>';
        return;
    }

    const computed = (state && designPbarg != null && designTc != null && !Number.isNaN(designPbarg) && !Number.isNaN(designTc))
        ? computeWallThicknessRows(state, designPbarg, designTc)
        : null;

    const fmtMm = v => (v == null || Number.isNaN(v)) ? '—' : v.toFixed(3);
    const blank = '—';

    if (computed) {
        tbody.innerHTML = computed.map(r => {
            const validClass  = r.validity === 'ALERT' ? 'wt-alert' : (r.validity === 'OK' ? 'wt-ok' : '');
            const schDisp     = r.sch_display != null ? r.sch_display : blank;
            const selThkDisp  = r.sel_thk_mm != null ? r.sel_thk_mm.toFixed(2) : blank;
            const statusDisp  = r.sch_status != null ? r.sch_status : blank;
            const statusClass = r.sch_status === 'OK' ? 'wt-ok' : (r.sch_status === 'NOT OK' ? 'wt-alert' : '');

            return `
                <tr>
                    <td>${escapeHtml(r.nps)}</td>
                    <td>${escapeHtml(String(r.od_mm))}</td>
                    <td>${fmtMm(r.t_mm)}</td>
                    <td>${fmtMm(r.d_over_6)}</td>
                    <td class="${validClass}">${r.validity || blank}</td>
                    <td>${fmtMm(r.tm_mm)}</td>
                    <td>${(r.mill_tol * 100).toFixed(1)}%</td>
                    <td>${fmtMm(r.calc_thk_mm)}</td>
                    <td>${escapeHtml(String(schDisp))}</td>
                    <td>${escapeHtml(selThkDisp)}</td>
                    <td class="${statusClass}">${escapeHtml(statusDisp)}</td>
                </tr>
            `;
        }).join('');
        return;
    }

    // Fallback before design conditions are set — NPS + D only.
    const dashes = `<td>${blank}</td>`.repeat(9);
    tbody.innerHTML = dims.rows.map(r => `
        <tr>
            <td>${escapeHtml(r.nps)}</td>
            <td>${escapeHtml(String(r.od_mm))}</td>
            ${dashes}
        </tr>
    `).join('');
}

// ---------------------------------------------------------------------------
// Engineering Requirements & Flags
//
// Pure function over the current state — every flag is one of four levels:
//   critical  (red)     — calculation can't proceed safely
//   mandatory (amber)   — design rule the engineer must apply
//   warning   (yellow)  — design point at the edge of validity
//   note      (blue)    — informational, no action needed
//
// Re-evaluated on every refresh so flags appear/disappear as inputs change.
// ---------------------------------------------------------------------------
const FLAG_LEVEL_LABEL = {
    critical:  'Critical',
    mandatory: 'Mandatory',
    warning:   'Warning',
    note:      'Note',
};

function evaluateFlags(state, designPbarg, designTc, mdmtC) {
    const flags = [];
    const cf       = (state && state.codeFactors) || {};
    const mat      = state && state.material ? state.material : '';
    const cleanMat = cleanMaterial(mat);
    const isAustenitic = cf.y_curve && cf.y_curve.category === 'austenitic_steels';

    // Critical — material has no entry in B31.3 Table A-1 (composites, plastics).
    if (!cf.stress_table) {
        flags.push({
            level: 'critical',
            title: 'No ASME B31.3 Allowable Stress',
            body:  `Material ${cleanMat} is not tabulated in B31.3 Table A-1 — likely composite or non-metal pipe (e.g. GRE per ISO 14692, CPVC per ASTM F441). Wall thickness cannot be computed from Eq. 3a; refer to material-specific design rules.`,
        });
    }

    // Critical — cold-end stress is clamped above the table maximum.
    if (cf.stress_at_cold && cf.stress_at_cold.clamped === 'high') {
        flags.push({
            level: 'critical',
            title: 'Cold-End Stress Clamped — Above Table Maximum',
            body:  `Cold temperature exceeds the highest tabulated point in B31.3 Table A-1 for this spec. The Allowable Stress shown is the table ceiling — verify the cold rated point falls within the material's published range.`,
        });
    }

    // Mandatory — NACE MR0175 sour service.
    if (/NACE/i.test(mat)) {
        flags.push({
            level: 'mandatory',
            title: 'NACE MR0175 / ISO 15156 — Sour Service',
            body:  'Material hardness controlled per NACE MR0175 (HRC ≤ 22 base metal, HV ≤ 250 weld). Heat-treatment certification, HIC / SSC qualification required. Project policy caps service temperature at 250°C for sour-service lines.',
        });
    }

    // Mandatory — Low-temperature carbon steel.
    if (/^LTCS/i.test(mat)) {
        const md = (mdmtC != null && !Number.isNaN(mdmtC)) ? `${mdmtC.toFixed(0)}°C` : 'MDMT';
        flags.push({
            level: 'mandatory',
            title: 'Low-Temperature Service — A333 Gr 6',
            body:  `Charpy V-notch impact testing per ASTM A333 Gr 6 — minimum 13.5 ft·lbf at ${md}. Welding procedure qualification at MDMT is mandatory; PWHT records to be retained.`,
        });
    }

    // Note — high-pressure CS NACE auto-promoted to API 5L X60 PSL-2.
    if (cf.stress_table && cf.stress_table.key === 'API5LX60') {
        flags.push({
            level: 'note',
            title: 'API 5L X60 PSL-2 Promoted (High-Pressure NACE)',
            body:  'For 1500# / 2500# CS NACE classes, project convention specs API 5L Grade X60 PSL-2 line pipe (S = 25,000 psi cold) instead of A106 Gr B (S = 20,000 psi). Wall thickness uses the X60 stress curve.',
        });
    }

    // Warning — Y above the ferritic baseline.
    if (designTc != null && designTc > 482 && !isAustenitic) {
        flags.push({
            level: 'warning',
            title: 'Y Coefficient — Above Ferritic Baseline (482°C)',
            body:  `Design temperature ${designTc.toFixed(0)}°C exceeds the 482°C ferritic baseline in B31.3 Table 304.1.1. Y rises in steps (0.4 → 0.5 at 510°C → 0.7 at 538°C). Verify the interpolation against your actual material spec.`,
        });
    }

    // Warning — W factor below 1 above 510°C creep onset.
    if (designTc != null && designTc > 510) {
        flags.push({
            level: 'warning',
            title: 'W Factor — Above Creep Onset (510°C)',
            body:  `Design temperature ${designTc.toFixed(0)}°C exceeds the 510°C creep onset. W = 1 still applies for seamless / 100% RT welded pipe; for lower joint efficiencies W < 1 per Table 302.3.5 — review weld strength reduction.`,
        });
    }

    // Warning — galvanizing temperature limit.
    if (/GALV/i.test(mat) && designTc != null && designTc > 200) {
        flags.push({
            level: 'warning',
            title: 'Galvanizing — Above Coating Temperature Limit',
            body:  `Design temperature ${designTc.toFixed(0)}°C exceeds the typical hot-dip galvanized zinc coating limit (~200°C). Coating may degrade in service — verify temperature compatibility or specify an alternative coating system.`,
        });
    }

    // Note — operating estimate is just a rule of thumb.
    if (cf.stress_table) {
        flags.push({
            level: 'note',
            title: 'Operating Conditions — 80% Estimate',
            body:  'The "Operating (est. 80%)" pressure / temperature on the Derived Design Conditions card is a rule-of-thumb estimate (0.8 × design). Replace with actual process operating point when available.',
        });
    }

    return flags;
}

function renderFlags(flags) {
    const container = document.getElementById('rEngineeringFlags');
    if (!container) return;
    if (!flags.length) {
        container.innerHTML = `<div class="flag-empty">&#10003; All clear — no engineering flags raised for this configuration.</div>`;
        return;
    }
    container.innerHTML = flags.map(f => `
        <div class="flag-card flag-card-${f.level}">
            <div class="flag-card-header">
                <span class="flag-badge flag-badge-${f.level}">${escapeHtml(FLAG_LEVEL_LABEL[f.level] || f.level)}</span>
                <span class="flag-title">${escapeHtml(f.title)}</span>
            </div>
            <div class="flag-body">${escapeHtml(f.body)}</div>
        </div>
    `).join('');
}

// B31.3 Eq. 3a worked-example card. Pure render, no I/O — pulls from
// the same state + code_factors as populateScheduleHeader. Picks NPS 6"
// as the example size (typical primary line) and computes both cases
// in inches, since that's how B31.3 conventions read (Table A-1 stress
// in psi, c in inches, etc.). Re-rendered on every refresh.
function renderFormulaCard(state, designPbarg, designTc) {
    const card = document.getElementById('rFormulaCard');
    if (!card) return;

    const dims = window._npsDimensions;
    const npsRow = dims && dims.rows
        ? dims.rows.find(r => r.nps_decimal === 6.0)
        : null;
    if (!npsRow) {
        card.innerHTML = '<div class="formula-card-empty">Worked example unavailable — NPS 6 not in dimensions.</div>';
        return;
    }

    const D_mm = npsRow.od_mm;
    const D_in = D_mm / 25.4;

    const cf          = (state && state.codeFactors) || {};
    const stressTable = cf.stress_table;
    const yCurve      = cf.y_curve;

    const coldPbarg = state.pt && state.pt.cold_point ? state.pt.cold_point.pressure_barg : null;
    const coldTc    = state.pt && state.pt.temperatures_c && state.pt.temperatures_c.length
                      ? state.pt.temperatures_c[0] : null;
    const coldTlbl  = state.pt && state.pt.temp_labels && state.pt.temp_labels.length
                      ? state.pt.temp_labels[0] : '—';

    const P1_psi = coldPbarg != null ? bargToPsig(coldPbarg) : null;
    const P2_psi = designPbarg != null ? bargToPsig(designPbarg) : null;

    const sCold = stressTable && coldTc != null ? lookupStress(stressTable, coldTc) : null;
    const sHot  = stressTable && designTc != null ? lookupStress(stressTable, designTc) : null;
    const S1 = sCold ? sCold.stress_psi : null;
    const S2 = sHot  ? sHot.stress_psi  : null;

    const joint   = document.getElementById('rJointType')?.value || 'Seamless';
    const E       = jointEfficiencyFromLabel(joint);
    const yLook   = lookupY(yCurve, designTc);
    const Y       = yLook ? yLook.y : 0.4;
    const yLabel  = yCurve ? (yCurve.label || 'unknown') : 'unknown';
    const W       = (designTc != null && designTc <= 510) ? 1.0 : NaN;

    const C_mm    = parseCorrosionMm(state.ca);
    const C_in    = C_mm / 25.4;
    const millTol = 0.125;

    // Eq. 3a per case in inches: t = P·D / [2·(S·E·W + P·Y)]
    let t1_in = null, t2_in = null;
    if (P1_psi != null && S1 != null && Number.isFinite(W)) {
        t1_in = (P1_psi * D_in) / (2 * (S1 * E * W + P1_psi * Y));
    }
    if (P2_psi != null && S2 != null && Number.isFinite(W)) {
        t2_in = (P2_psi * D_in) / (2 * (S2 * E * W + P2_psi * Y));
    }

    // Determine governing case by t_press magnitude (which encodes both P and S).
    const candidates = [t1_in, t2_in].filter(v => v != null && Number.isFinite(v));
    if (!candidates.length) {
        card.innerHTML = `
            <div class="formula-card-title">ASME B31.3 §304.1.2 — Internal Pressure (Eq. 3a, enhanced with W-factor)</div>
            <div class="formula-card-eq">t<sub>req</sub> = (P × OD) / [2 × (S × E × W + P × Y)] + c</div>
            <div class="formula-card-empty">Worked example needs allowable stress — not available for this material.</div>
        `;
        return;
    }
    const case1Governs = (t1_in != null && (t2_in == null || t1_in >= t2_in));
    const t_press_in = case1Governs ? t1_in : t2_in;
    const tm_in = t_press_in + C_in;
    const T_in  = tm_in / (1 - millTol);
    const T_mm  = T_in * 25.4;

    const fmtIn  = v => v == null ? '—' : v.toFixed(4) + '"';
    const fmtPsi = v => v == null ? '—' : v.toLocaleString();
    const govSpan = '<span class="formula-tag-governs">← GOVERNS</span>';

    const designTfmt = designTc != null && !Number.isNaN(designTc) ? designTc.toFixed(0) : '—';
    const coldDeg = coldTc != null ? `${coldTlbl}°C` : '—';

    card.innerHTML = `
        <div class="formula-card-title">ASME B31.3 §304.1.2 — Internal Pressure (Eq. 3a, enhanced with W-factor)</div>
        <div class="formula-card-eq">t<sub>req</sub> = (P × OD) / [2 × (S × E × W + P × Y)] + c</div>
        <div class="formula-card-example">
            <div>
                <strong>NPS ${escapeHtml(npsRow.nps)}" example:</strong>
                OD = ${D_in.toFixed(3)}"
                | E = ${E}
                | W = ${Number.isFinite(W) ? W : '—'}
                | Y = ${Y.toFixed(2)} <span class="kv-tag-inline dim">[${escapeHtml(yLabel)}]</span>
                | c = ${C_in.toFixed(4)}" <span class="kv-tag-inline dim">(<span class="formula-num">${C_mm} mm</span>)</span>
                | mill tol = <span class="formula-num">${(millTol*100).toFixed(1)}%</span>
            </div>
            <div>
                <strong>Case 1 (Min T / Max P @ ${escapeHtml(coldDeg)}):</strong>
                P = ${P1_psi != null ? P1_psi.toFixed(1) : '—'} psig,
                S = ${fmtPsi(S1)} psi
                → t<sub>press</sub> = <span class="formula-num">${fmtIn(t1_in)}</span>
                ${case1Governs ? govSpan : ''}
            </div>
            <div>
                <strong>Case 2 (Design Point @ ${designTfmt}°C):</strong>
                P = ${P2_psi != null ? P2_psi.toFixed(1) : '—'} psig,
                S = ${fmtPsi(S2)} psi
                → t<sub>press</sub> = <span class="formula-num">${fmtIn(t2_in)}</span>
                ${!case1Governs && t2_in != null ? govSpan : ''}
            </div>
            <div class="formula-final">
                Using ${case1Governs ? 'Case 1 (Min T / Max P)' : 'Case 2 (Design Point)'}:
                t = ${fmtIn(t_press_in)} → t<sub>m</sub> = t+c = ${fmtIn(tm_in)}
                → T<sub>REQ</sub> = t<sub>m</sub>/(1−${(millTol*100).toFixed(1)}%) = <strong>${fmtIn(T_in)}</strong>
                (${T_mm.toFixed(2)} mm)
            </div>
        </div>
        <div class="formula-card-notes">
            • t<sub>min</sub> = (t<sub>req</sub> + c) / (1 − 12.5%)
            &nbsp;|&nbsp;
            • MAWP = [2×S×E×W×t<sub>eff</sub>] / [OD − 2×Y×t<sub>eff</sub>]
            &nbsp;|&nbsp;
            • t<sub>eff</sub> = WT<sub>nom</sub> × (1 − mill%) − c − mech
        </div>
    `;
}

function populateScheduleHeader(state, designPbarg, designTc, mdmtC) {
    // --- Service strip -----------------------------------------------------
    const services = (state.service || '').split(',').map(s => s.trim()).filter(Boolean);
    const strip = document.getElementById('rServiceStrip');
    if (strip) {
        strip.innerHTML = `
            <span class="service-strip-label">Service:</span>
            ${services.length
                ? services.map(s => `<span class="service-pill">${escapeHtml(s)}</span>`).join('')
                : '<span class="service-strip-empty">— none —</span>'}
        `;
    }

    // --- Pressure values ---------------------------------------------------
    // Cold case: max rated pressure across the indexed envelope (typically
    // the value in the lowest-temperature column).
    const coldPbarg = state.pt && state.pt.cold_point ? state.pt.cold_point.pressure_barg : null;
    const coldPpsig = coldPbarg != null ? bargToPsig(coldPbarg) : null;
    const coldTlbl  = state.pt && state.pt.temp_labels && state.pt.temp_labels.length
        ? state.pt.temp_labels[0] : '—';
    // Cold-end design temperature (numeric, °C) for stress lookup. The
    // P-T table's first temp column is what the Excel uses as Case 1
    // (e.g. "-29 to 38" → use 38°C as the upper bound for stress).
    const coldTc = state.pt && state.pt.temperatures_c && state.pt.temperatures_c.length
        ? state.pt.temperatures_c[0] : null;

    // Design pressure as entered by the user.
    const dPpsig = bargToPsig(designPbarg);

    // GOVERNS marker on pressure: the case with the larger P drives wall
    // thickness via Eq. 3a (without S yet — once stresses are wired the
    // proper compare is on t/D ratio per case, MAX wins).
    const coldGoverns = coldPbarg != null && coldPbarg > designPbarg;

    // --- Material info -----------------------------------------------------
    const cleanMat  = cleanMaterial(state.material);
    const isLowTemp = /^LTCS/i.test(state.material);
    const isSS      = /^(SS|316|6\s*MO)/i.test(cleanMat);

    // --- Joint type (read from Tab 2 input) --------------------------------
    const joint = document.getElementById('rJointType')?.value || 'Seamless';
    const E     = jointEfficiencyFromLabel(joint);

    // --- Pipe standard rule of thumb --------------------------------------
    // Carbon steel / LTCS / DSS / SDSS / Ti / Cu / CuNi etc. → B36.10M.
    // Stainless schedules (5S/10S/40S/80S) live in B36.19M for ≤ 12";
    // larger SS sizes drop back to B36.10M dimensions.
    const pipeStandard = isSS
        ? 'ASME B36.19M (≤12") / B36.10M (>12")'
        : 'ASME B36.10M';

    // --- Y coefficient (B31.3 Table 304.1.1) -------------------------------
    // Real interpolated lookup against y_coefficient.json. The table goes
    // 0.4 (≤482°C) → 0.5 → 0.7 in steps; we interpolate between published
    // breakpoints. The category label (Ferritic / Austenitic / etc.) comes
    // from the same JSON.
    const designTf = cToF(designTc);
    const yLook    = state.codeFactors && state.codeFactors.y_curve
                     ? lookupY(state.codeFactors.y_curve, designTc)
                     : null;
    const yCategoryLabel = state.codeFactors && state.codeFactors.y_curve
                           ? state.codeFactors.y_curve.label
                           : 'unknown category';
    let yCoef = yLook ? yLook.y : NaN;
    let yNote = yLook
        ? `per ASME B31.3 Table 304.1.1 @ ${fmt(designTf, 1)}°F (${yCategoryLabel})`
        : `pending — no Y curve indexed for this material`;
    if (yLook && yLook.clamped === 'high') {
        yNote = `clamped — design T ${fmt(designTc, 0)}°C above table max; consult Table 304.1.1`;
    }

    // --- W-factor (B31.3 Table 302.3.5) ------------------------------------
    // Stays 1 below 510°C for seamless / 100% RT welded. Drops with T for
    // certain joint types — stub out the warning until the full curve is in.
    let wFactor = 1;
    let wNote   = `per ASME B31.3 Table 302.3.5 @ ${fmt(designTf, 1)}°F (W=1)`;
    if (designTc > 510) {
        wFactor = NaN;
        wNote   = `pending — design T ${fmt(designTc, 0)}°C above the 510°C creep onset; W < 1 per Table 302.3.5`;
    }

    // --- Helpers ----------------------------------------------------------
    const govSpan    = '<span class="kv-tag-inline governs">[GOVERNS]</span>';
    const activeSpan = '<span class="kv-tag-inline active">[active]</span>';
    const dimSpan    = (txt) => `<span class="kv-tag-inline dim">[${escapeHtml(txt)}]</span>`;

    const pendingValue = (msg) =>
        `<span class="kv-value" style="color:var(--text-muted);font-style:italic">${escapeHtml(msg)}</span>`;

    // --- Build Design Parameters card --------------------------------------
    const ratingNum = state.rating.replace('#', '').trim();
    const classCell = `<span class="kv-value bold">${escapeHtml(state.classCode)} (${escapeHtml(state.rating)})</span>`;

    const pressureCell = coldPbarg != null
        ? `
            <div class="kv-multi">
                <div>
                    <strong>Min T / Max P:</strong> ${fmt(coldPpsig, 1)} psig
                    <span class="unit">(${fmt(coldPbarg, 1)} barg)</span>
                    <span class="unit">@ ${escapeHtml(coldTlbl)}</span>
                    ${coldGoverns ? govSpan : activeSpan}
                </div>
                <div>
                    <strong>Design Point:</strong> ${fmt(dPpsig, 1)} psig
                    <span class="unit">(${fmt(designPbarg, 1)} barg)</span>
                    <span class="unit">@ ${fmt(designTc, 0)}°C</span>
                    ${coldGoverns ? activeSpan : govSpan}
                </div>
                <div class="kv-foot">t<sub>REQ</sub> uses MAX(Case 1, Case 2) per size</div>
            </div>
          `
        : `
            <div class="kv-multi">
                <div>
                    <strong>Design Point:</strong> ${fmt(dPpsig, 1)} psig
                    <span class="unit">(${fmt(designPbarg, 1)} barg)</span>
                    <span class="unit">@ ${fmt(designTc, 0)}°C</span>
                    ${govSpan}
                </div>
                <div class="kv-foot">No P-T envelope indexed — design point governs by default</div>
            </div>
          `;

    const tempCell = `
        <div class="kv-multi">
            <div>
                <strong>Min:</strong> ${escapeHtml(coldTlbl)}
                <span class="unit">(${state.pt && state.pt.temperatures_c ? fmt(cToF(state.pt.temperatures_c[0]), 1) : '—'}°F)</span>
                ${dimSpan('P-T min')} ${coldGoverns ? govSpan : activeSpan}
            </div>
            <div>
                <strong>Max (Design):</strong> ${fmt(designTc, 0)}°C
                <span class="unit">(${fmt(designTf, 1)}°F)</span>
                ${dimSpan('design')} ${coldGoverns ? activeSpan : govSpan}
            </div>
        </div>
    `;

    // --- Allowable Stress S(T), B31.3 Table A-1 ----------------------------
    // S₁ is at the cold P-T endpoint (typically the 38°C/100°F column).
    // S₂ is at the user's design temperature, interpolated.
    // GOVERNS marker on stress mirrors the pressure GOVERNS — whichever
    // case has the higher pressure wins. Proper t/D compare across the
    // two cases will land when the wall-thickness view is built.
    const stressTable = state.codeFactors && state.codeFactors.stress_table
                        ? state.codeFactors.stress_table
                        : null;
    const sCold = stressTable && coldTc != null ? lookupStress(stressTable, coldTc) : null;
    const sHot  = stressTable                   ? lookupStress(stressTable, designTc) : null;

    const tableLabel = stressTable ? stressTable.label : cleanMat;
    const stressCell = stressTable
        ? `
            <div class="kv-multi">
                <div>
                    <strong>S @ ${escapeHtml(coldTlbl)}:</strong>
                    ${sCold ? fmt(sCold.stress_psi, 0).replace(/(\d)(?=(\d{3})+$)/g, '$1,') + ' psi' : '—'}
                    <span class="unit">(${sCold ? fmt(sCold.stress_mpa, 1) + ' MPa' : '—'})</span>
                    ${coldGoverns ? govSpan : activeSpan}
                </div>
                <div>
                    <strong>S @ ${fmt(designTc, 0)}°C:</strong>
                    ${sHot ? fmt(sHot.stress_psi, 0).replace(/(\d)(?=(\d{3})+$)/g, '$1,') + ' psi' : '—'}
                    <span class="unit">(${sHot ? fmt(sHot.stress_mpa, 1) + ' MPa' : '—'})</span>
                    ${sHot && sHot.clamped === 'high' ? '<span class="kv-tag-inline dim">[clamped — above table max]</span>' : ''}
                    ${coldGoverns ? activeSpan : govSpan}
                </div>
                <div class="kv-foot">per ASME B31.3 Table A-1 [${escapeHtml(stressTable.key || cleanMat)}]</div>
            </div>
          `
        : `
            <div class="kv-multi">
                <div>${pendingValue('S @ ' + coldTlbl + ': no B31.3 Table A-1 entry for this material')}</div>
                <div>${pendingValue('S @ ' + fmt(designTc, 0) + '°C: no B31.3 Table A-1 entry')}</div>
                <div class="kv-foot">B31.3 doesn't tabulate stress for ${escapeHtml(cleanMat)} (likely composite / non-metal)</div>
            </div>
          `;

    document.getElementById('rDesignParamsList').innerHTML = [
        `<div class="kv-row"><span class="kv-label">PMS Class</span>${classCell}</div>`,
        `<div class="kv-row top"><span class="kv-label">Design Pressure (P)</span>${pressureCell}</div>`,
        `<div class="kv-row top"><span class="kv-label">Design Temperature</span>${tempCell}</div>`,
        `<div class="kv-row"><span class="kv-label">Material</span><span class="kv-value bold">${escapeHtml(cleanMat)}</span></div>`,
        `<div class="kv-row"><span class="kv-label">Material Spec</span>${pendingValue('— pending data source —')}</div>`,
        `<div class="kv-row top"><span class="kv-label">Allowable Stress S(T)</span>${stressCell}</div>`,
    ].join('');

    // --- Build Fabrication & Code Factors card -----------------------------
    const yCell = Number.isFinite(yCoef)
        ? `<span class="kv-value bold">${fmt(yCoef, 2)}</span> <span class="kv-foot inline">${escapeHtml(yNote)}</span>`
        : pendingValue(yNote);

    const wCell = Number.isFinite(wFactor)
        ? `<span class="kv-value bold">${wFactor}</span> <span class="kv-foot inline">${escapeHtml(wNote)}</span>`
        : pendingValue(wNote);

    document.getElementById('rCodeFactorsList').innerHTML = [
        `<div class="kv-row"><span class="kv-label">Pipe Standard</span><span class="kv-value bold">${escapeHtml(pipeStandard)}</span></div>`,
        `<div class="kv-row"><span class="kv-label">Joint Type</span><span class="kv-value bold">${escapeHtml(joint)}</span></div>`,
        `<div class="kv-row"><span class="kv-label">Joint Efficiency (E)</span><span class="kv-value bold">${fmt(E, 2).replace(/\.00$/, '')}</span></div>`,
        `<div class="kv-row top"><span class="kv-label">Y Coefficient</span>${yCell}</div>`,
        `<div class="kv-row top"><span class="kv-label">W-factor (Weld Str.)</span>${wCell}</div>`,
        `<div class="kv-row"><span class="kv-label">Corrosion Allow. (c)</span><span class="kv-value bold">${escapeHtml(state.ca)}</span></div>`,
        `<div class="kv-row"><span class="kv-label">Mill Undertolerance</span><span class="kv-value bold">${(REPORT_CONST.millTolerance * 100).toFixed(1)}%</span></div>`,
    ].join('');
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
        // Tab 3 reads the same design conditions — keep it in sync even
        // when the user is on Tab 2, so switching to Tab 3 never shows stale.
        populateScheduleHeader(state, dp, dt, md);
        populateWallThicknessTable(state, dp, dt);
        renderFormulaCard(state, dp, dt);
        renderFlags(evaluateFlags(state, dp, dt, md));
        renderSummaryStats(state, dp, dt);
        renderTagLegend(state);
        renderPipeFittingsTab(state, dp, dt);
        renderTab5Components(state);
        renderDatasheetTab(state, dp, dt);
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
    // Dashboard layout: hide empty state, show report pane + design-conditions
    // sidebar section. Inputs remain visible (sidebar is sticky).
    const empty = document.getElementById('emptyState');
    if (empty) empty.style.display = 'none';
    document.getElementById('reportPanel').style.display = 'block';
    const dl = document.getElementById('sidebarDesign');
    if (dl) dl.style.display = '';
    const dx = document.getElementById('downloadExcelBtn');
    if (dx) dx.disabled = false;

    // Pre-fill the editable design conditions ONLY if the user hasn't
    // typed anything yet — preserve their edits across re-resolves.
    const pIn = document.getElementById('rDesignPressure');
    const pPsig = document.getElementById('rDesignPressurePsig');
    const tIn = document.getElementById('rDesignTemperature');
    if (pIn && !pIn.value) pIn.value = fmt(state.designP, 1);
    if (pPsig && !pPsig.value) pPsig.value = fmt(bargToPsig(state.designP), 1);
    if (tIn && !tIn.value) tIn.value = fmt(state.designT, 0);

    populateBanner(state);
    populateStandardBar(state);
    wireReportInputs(state);
    wireReportTabs();
    // Wall Thickness table + formula card both depend on the NPS list
    // and B36.10M Table 2-1 (cached after first fetch). wireReportInputs
    // already fired a synchronous refresh above before these resolved,
    // so re-render both once data lands. Subsequent refreshes (driven
    // by input edits) will hit the cache and work first try.
    Promise.all([
        ensureNpsDimensions(),
        ensurePipeDimensions(),
        ensurePipeDimensionsSs(),
    ]).then(() => {
        populateWallThicknessTable(state, state.designP, state.designT);
        renderFormulaCard(state, state.designP, state.designT);
        renderSummaryStats(state, state.designP, state.designT);
        renderTagLegend(state);
        renderPipeFittingsTab(state, state.designP, state.designT);
        renderDatasheetTab(state, state.designP, state.designT);
    });
    // Only force-reset to Tab 1 on the very first show — subsequent resolves
    // (changing inputs while the report is up) preserve the user's tab.
    if (!window._reportShown) {
        document.querySelectorAll('.report-tab').forEach((t, i) => t.classList.toggle('active', i === 0));
        document.querySelectorAll('.report-tab-content').forEach((c, i) => c.classList.toggle('active', i === 0));
        window._reportShown = true;
    }
}

function hideReport() {
    document.getElementById('reportPanel').style.display = 'none';
    const empty = document.getElementById('emptyState');
    if (empty) empty.style.display = '';
    const dl = document.getElementById('sidebarDesign');
    if (dl) dl.style.display = 'none';
    const dx = document.getElementById('downloadExcelBtn');
    if (dx) dx.disabled = true;
    window._reportShown = false;
}

function wireForm() {
    // Sidebar toggle (mobile / narrow screens)
    const toggle = document.getElementById('sidebarToggle');
    const sidebar = document.getElementById('sidebar');
    if (toggle && sidebar) {
        toggle.addEventListener('click', () => {
            sidebar.classList.toggle('collapsed');
            sidebar.classList.toggle('open');
        });
    }

    // Download Excel — pulls live design conditions from Tab 2 inputs so
    // the export reflects whatever the user is currently looking at.
    const xlsxBtn = document.getElementById('downloadExcelBtn');
    if (xlsxBtn) {
        xlsxBtn.addEventListener('click', async () => {
            const cached = window._lastResolution;
            if (!cached) {
                showToast('Resolve a class first.', 'error');
                return;
            }
            const dp = parseFloat(document.getElementById('rDesignPressure')?.value);
            const dt = parseFloat(document.getElementById('rDesignTemperature')?.value);
            const md = parseFloat(document.getElementById('rMdmt')?.value);
            const joint = document.getElementById('rJointType')?.value || 'Seamless';

            const rating = document.getElementById('pipingClass').value.trim();
            const material = document.getElementById('material').value.trim();
            const ca = document.getElementById('corrosionAllowance').value.trim();
            const service = document.getElementById('service').value.trim();

            xlsxBtn.disabled = true;
            const origLabel = xlsxBtn.innerHTML;
            xlsxBtn.innerHTML = 'Generating…';

            try {
                const res = await fetch('/api/export/excel', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        rating, material, ca, service,
                        design_p_barg: dp, design_t_c: dt,
                        mdmt_c: Number.isNaN(md) ? -29 : md,
                        joint_type: joint,
                    }),
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.detail || `HTTP ${res.status}`);
                }
                const blob = await res.blob();
                // Extract filename from Content-Disposition if present.
                const disp = res.headers.get('Content-Disposition') || '';
                const m = disp.match(/filename="([^"]+)"/);
                const filename = m ? m[1] : `PMS-${cached.class_code}.xlsx`;

                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url; a.download = filename;
                document.body.appendChild(a); a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
                showToast(`Downloaded ${filename}`, 'success', 4000);
            } catch (e) {
                showToast(`Export failed: ${e.message || e}`, 'error', 6000);
            } finally {
                xlsxBtn.disabled = false;
                xlsxBtn.innerHTML = origLabel;
            }
        });
    }
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

// ---------------------------------------------------------------------------
// Allowable Stress S(T) — JS-side interpolation against the table the
// backend hands us in resolution.code_factors.stress_table.
//
// Mirror of stress_lookup.lookup() in Python: linear interpolate at temp_c,
// clamp at endpoints, round to nearest 100 psi. Both sides return identical
// numbers so the report stays consistent across page refreshes.
// ---------------------------------------------------------------------------
const PSI_TO_MPA = 0.00689476;

function _interpolateAtTempC(byTempC, targetT) {
    const keys = Object.keys(byTempC || {}).map(Number).sort((a, b) => a - b);
    if (!keys.length) return { value: null, clamped: null };
    if (targetT <= keys[0])              return { value: byTempC[String(keys[0])], clamped: targetT < keys[0] ? 'low' : null };
    if (targetT >= keys[keys.length-1])  return { value: byTempC[String(keys[keys.length-1])], clamped: targetT > keys[keys.length-1] ? 'high' : null };
    for (let i = 0; i < keys.length - 1; i++) {
        const t1 = keys[i], t2 = keys[i+1];
        if (targetT >= t1 && targetT <= t2) {
            const v1 = byTempC[String(t1)], v2 = byTempC[String(t2)];
            const frac = (targetT - t1) / (t2 - t1);
            return { value: v1 + frac * (v2 - v1), clamped: null };
        }
    }
    return { value: byTempC[String(keys[keys.length-1])], clamped: 'high' };
}

function lookupStress(stressTable, tempC) {
    if (!stressTable || !stressTable.stress_psi_by_temp_c || tempC == null) return null;
    const { value, clamped } = _interpolateAtTempC(stressTable.stress_psi_by_temp_c, tempC);
    if (value == null) return null;
    const rounded = Math.round(value / 100) * 100;
    return {
        stress_psi: rounded,
        stress_mpa: Math.round(rounded * PSI_TO_MPA * 10) / 10,
        clamped,
    };
}

function lookupY(yCurve, tempC) {
    if (!yCurve || !yCurve.y_values || tempC == null) return null;
    const temps = yCurve.temperatures_c || [];
    const yvals = yCurve.y_values;
    // Pair only published entries (gray iron's trailing nulls are dropped).
    const byTempC = {};
    for (let i = 0; i < temps.length; i++) {
        if (yvals[i] != null) byTempC[String(temps[i])] = yvals[i];
    }
    const { value, clamped } = _interpolateAtTempC(byTempC, tempC);
    if (value == null) return null;
    return { y: Math.round(value * 100) / 100, clamped };
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

    // Compact sidebar preview — class code prominent + small input pills.
    const pills = [
        inputs.rating,
        (typeof cleanMaterial === 'function' ? cleanMaterial(inputs.material) : inputs.material),
        inputs.ca,
    ].filter(Boolean).map(p => `<span class="ds-class-pill">${escapeHtml(p)}</span>`).join('');

    panel.innerHTML = `
        <div class="ds-class-code">${escapeHtml(data.class_code)}</div>
        <div class="ds-class-meta">Resolved §5.5 class</div>
        <div class="ds-class-pills">${pills}</div>
    `;

    // Also surface the class code in the top-nav center for orientation.
    const navStatus = document.getElementById('navStatus');
    if (navStatus) {
        navStatus.innerHTML = `<span class="nav-status-pill">${escapeHtml(data.class_code)}</span>`;
    }

    // Seed design-conditions defaults so showReport() can pre-fill if empty.
    // Cold-rated point is the safest starting design pressure.
    const pt = data.pressure_temperature || {};
    const coldP = (pt.cold_point && pt.cold_point.pressure_barg) || (pt.pressures_barg || [0])[0] || 0;
    const designT = (pt.temperatures_c || [38])[0] || 38;

    const state = {
        rating:      inputs.rating,
        material:    inputs.material,
        ca:          inputs.ca,
        service:     inputs.service || '',
        classCode:   data.class_code,
        pt:          data.pressure_temperature,
        codeFactors: data.code_factors || null,
        designP:     coldP,
        designT:     designT,
    };
    // Cache for the Excel-download handler and any re-render.
    window._currentState = state;
    showReport(state);
}

function renderResolutionError(panel, message) {
    panel.style.display = 'block';
    panel.classList.remove('derived');
    panel.classList.add('error');
    panel.innerHTML = `
        <div class="ds-class-code" style="font-size:0.95rem">Cannot resolve</div>
        <div class="ds-class-meta">${escapeHtml(message)}</div>
    `;
    // Hide report + revert to empty state until inputs are valid again.
    hideReport();
    const navStatus = document.getElementById('navStatus');
    if (navStatus) navStatus.innerHTML = '';
}

function initClassResolver() {
    const panel = document.getElementById('classResolution');
    const rating   = document.getElementById('pipingClass');
    const material = document.getElementById('material');
    const ca       = document.getElementById('corrosionAllowance');
    const service  = document.getElementById('service');
    if (!panel || !rating || !material || !ca) return;

    // Sequence-protect against fast input edits: only the latest request wins.
    let seq = 0;

    async function tryResolve() {
        const r = rating.value.trim();
        const m = material.value.trim();
        const c = ca.value.trim();
        if (!r || !m || !c) {
            panel.style.display = 'none';
            hideReport();
            const navStatus = document.getElementById('navStatus');
            if (navStatus) navStatus.innerHTML = '';
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
            if (my !== seq) return;
            const data = await res.json();
            if (!res.ok) {
                renderResolutionError(panel, data.detail || `HTTP ${res.status}`);
                return;
            }
            window._lastResolution = data;
            renderResolution(panel, data, {
                rating:   r,
                material: m,
                ca:       c,
                service:  service ? service.value : '',
            });
        } catch (e) {
            if (my !== seq) return;
            renderResolutionError(panel, `Network error: ${e}`);
        }
    }

    [rating, material, ca].forEach(el => el.addEventListener('change', tryResolve));
    if (service) service.addEventListener('input', tryResolve);
}

document.addEventListener('DOMContentLoaded', () => {
    loadOptions().then(() => initClassResolver());
    wireForm();
});
