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
    npsDimensions:    (material, service) => {
        const params = [];
        if (material) params.push(`material=${encodeURIComponent(material)}`);
        if (service)  params.push(`service=${encodeURIComponent(service)}`);
        return fetch('/api/nps-dimensions' + (params.length ? `?${params.join('&')}` : ''));
    },
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
        const prev = hidden.value;
        hidden.value = joined;
        // Programmatic `value =` doesn't fire DOM events, so the resolver's
        // input listener wouldn't see the change. Dispatch a synthetic input
        // event when the joined value actually changes so the report re-runs.
        if (joined !== prev) {
            hidden.dispatchEvent(new Event('input', { bubbles: true }));
        }
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

    const effective = _dsEffectiveClassCode(state);
    banner.innerHTML = `
        <div class="pms-banner-header">Resolved §5.5 PMS Code</div>
        <div class="pms-banner-code">${escapeHtml(effective)}</div>
        <div class="pms-banner-details">${pillsHtml}</div>
        <div class="pms-banner-id">PMS-${escapeHtml(effective)}</div>
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

// Cap the on-screen P-T Rating table at this temperature. The
// underlying curve in `state.pt` still extends to the full ASME B16.5
// published max (538 / 450 / 400 °C for Groups 1.1 / 2.3 / 2.8) and
// drives interpolation, adequacy, WT calc, and the °F footnote — only
// the visible columns are capped, so the page reads at typical
// operating range without overwhelming the user with high-T entries.
const PT_TABLE_DISPLAY_CAP_C = 300;

function populatePtTable(state, designTc) {
    const tbl = document.getElementById('rPtTable');
    if (!state.pt || !state.pt.temperatures_c || !state.pt.pressures_barg) {
        tbl.innerHTML = `<tbody><tr><td style="padding:24px;text-align:center;color:var(--text-muted)">No P-T data indexed for this combination.</td></tr></tbody>`;
        return;
    }
    const fullTemps  = state.pt.temperatures_c;
    const fullPress  = state.pt.pressures_barg;
    const fullLabels = state.pt.temp_labels || fullTemps.map(String);

    // Filter to columns at or below the display cap. The cold-end
    // column (e.g. T=38 for the "-29 to 38" label) is always included
    // because 38 ≤ 300. Drop only the high-T columns.
    const temps  = [];
    const press  = [];
    const labels = [];
    for (let i = 0; i < fullTemps.length; i++) {
        if (fullTemps[i] <= PT_TABLE_DISPLAY_CAP_C) {
            temps.push(fullTemps[i]);
            press.push(fullPress[i]);
            labels.push(fullLabels[i]);
        }
    }

    // Locate the column whose stored temperature equals the design T (exact
    // match only — interpolation is shown in the derived box, not the table).
    const highlightIdx = temps.findIndex(t => Number(t) === Number(designTc));

    // Hydrotest is always 1.5 × the curve's overall max P (cold-end),
    // which is still inside the visible columns since cold-end ≤ 300 °C.
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
// Material-family key — drives per-material caching of NPS dimensions.
// CuNi (UNS C70600 / EEMUA 144) has different ODs at small bores than the
// generic ASME B36.10M list; everyone else falls back to "default".
function _npsKey(material, service) {
    if (!material) return 'default';
    if (/\bGRE\b|EPOXY\s*FIBRE|Glass.*Reinforced/i.test(material)) {
        return (service && /Hypochlorite|BONSTRAND/i.test(service))
            ? 'gre_bonstrand'
            : 'gre';
    }
    if (/CuNi|C70600|B466/i.test(material)) return 'cuni';
    if (/\bCOPPER\b|C12200|\bB42\b/i.test(material)) return 'copper';
    if (/\bCPVC\b/i.test(material)) return 'cpvc';
    if (/\bTITANIUM\b|\bTi\b|B861/i.test(material)) return 'titanium';
    if (/Tubing|N08367|6\s*MO/i.test(material)) return 'tubing';
    return 'default';
}

async function ensureNpsDimensions(material, service) {
    const key = _npsKey(material, service);
    window._npsDimsByKey = window._npsDimsByKey || {};
    if (window._npsDimsByKey[key]) {
        window._npsDimensions = window._npsDimsByKey[key];
        return window._npsDimensions;
    }
    try {
        const res = await API.npsDimensions(material, service);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        window._npsDimsByKey[key] = data;
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
    // Project-mandated override: if the active NPS dim file carries
    // explicit `sch` + `wt_mm` per NPS (e.g. Titanium A70), use those
    // directly so Tab 3 and Excel View both show the project-spec value
    // instead of a calc-derived pick.
    const dims = window._npsDimensions;
    if (dims && dims.rows) {
        const row = dims.rows.find(r => r.nps_decimal === npsDecimal);
        if (row && row.sch != null && row.wt_mm != null) {
            return {
                sch_display: String(row.sch),
                wt_mm:       row.wt_mm,
                status:      'OK',
                table:       'project-spec',
                row,
            };
        }
    }
    const useSs = _materialUsesStainlessSchedules(material);
    if (calcThkMm == null || Number.isNaN(calcThkMm)) return null;

    // Stainless materials: try B36.19M first (5S / 10S / 40S / 80S).
    // B36.19M tops out at 80S — high-pressure stainless classes (e.g. G10)
    // can have a calc thk exceeding that, in which case we fall back to
    // B36.10M's heavier walls (SCH 160 / XXS) because the OD is identical
    // (A312 stainless pipe can be specified to B36.10M wall per project
    // practice). Without this fallback the picker silently lands on a
    // wall THINNER than required.
    const primaryTable = useSs ? window._b3619 : window._b3610;
    const fallbackTable = useSs ? window._b3610 : null;

    function pickFrom(tableObj) {
        if (!tableObj || !tableObj.by_nps) return null;
        const rows = tableObj.by_nps[npsDecimal];
        if (!rows || !rows.length) return null;
        // Rows are sorted ascending by WT.
        const found = rows.find(r => r.wt_mm >= calcThkMm);
        return found || null;
    }

    let pick = pickFrom(primaryTable);
    let usedTable = useSs ? 'B36.19M' : 'B36.10M';
    if (!pick && fallbackTable) {
        pick = pickFrom(fallbackTable);
        if (pick) usedTable = 'B36.10M (fallback from B36.19M)';
    }

    let status = 'OK';
    if (!pick) {
        // No wall in either table covers calcThk — flag NOT OK and report
        // the heaviest available from the heavier table (fallback if any,
        // else primary). Avoids misleading "NOT OK at 80S 3.91" when the
        // user should see "NOT OK at XXS 7.82".
        const heavy = fallbackTable || primaryTable;
        const rows = (heavy && heavy.by_nps && heavy.by_nps[npsDecimal]) || [];
        if (!rows.length) return null;
        pick = rows[rows.length - 1];
        status = 'NOT OK';
    }

    const display = pick.schedule != null ? pick.schedule : (pick.identification || '—');
    return {
        sch_display: String(display),
        wt_mm:       pick.wt_mm,
        status,
        table:       usedTable,
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
        // Skip NOT OK rows — they don't carry a standard schedule; the
        // engineer specs a custom wall on those NPS sizes.
        if (r.sch_status === 'NOT OK') continue;
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

// NPS axis is derived from whatever is in nps_dimensions.json — no hardcoded
// ceiling, so adding NPS 36 / 42 / 48 to the data file flows through to the
// datasheet automatically.
function _dsNpsAxis() {
    const dims = window._npsDimensions;
    if (dims && dims.rows && dims.rows.length) {
        return dims.rows
            .map(r => r.nps_decimal)
            .filter(v => typeof v === 'number' && !Number.isNaN(v))
            .sort((a, b) => a - b);
    }
    // Fallback used only if NPS dimensions haven't loaded yet.
    return [0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 36];
}

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

function _isCuNiMat(material) { return /CUNI|C70600|B466/i.test(material || ''); }
function _isCopperMat(material) {
    const u = (material || '').toUpperCase();
    if (u.includes('CUNI')) return false;
    return /COPPER|C12200|\bB42\b/i.test(material || '');
}
function _isGreMat(material) {
    return /\bGRE\b|EPOXY\s*FIBRE|Glass.*Reinforced/i.test(material || '');
}
function _isCpvcMat(material) {
    return /\bCPVC\b/i.test(material || '');
}
function _isTitaniumMat(material) {
    return /\bTITANIUM\b|\bTi\b|B861/i.test(material || '');
}
function _isBonstrandService(service) {
    return /Hypochlorite|BONSTRAND/i.test(service || '');
}

// Class code to display — for most materials this is the resolver output
// (e.g. A1, F2N, G10). GRE is the exception: the project differentiates
// A50 / A51 / A52 by service even though §5.5 derives a single digit (50).
function _displayedClassCode(material, service, fallback) {
    if (!material || !_isGreMat(material)) return fallback || '';
    const svc = service || '';
    if (/Hypochlorite/i.test(svc)) return 'A51';
    if (/Special/i.test(svc))      return 'A52';
    return 'A50';
}
function _dsEffectiveClassCode(state) {
    if (!state) return '';
    return _displayedClassCode(state.material, state.service, state.classCode);
}

function _dsPipeType(material, service) {
    const u = (material || '').toUpperCase();
    if (_isGreMat(u)) {
        return _isBonstrandService(service)
            ? { sml: 'Manufacturer standard (BONSTRAND Series 50000C)',
                lrg: 'Manufacturer standard (BONSTRAND Series 50000C)', merge: true }
            : { sml: 'Manufacturer standard (TBA)',
                lrg: 'Manufacturer standard (TBA)', merge: true };
    }
    if (_isCuNiMat(u)) {
        return { sml: 'Seamless', lrg: 'Seam Welded', merge: false };
    }
    if (_isCopperMat(u)) {
        return { sml: 'Seamless Hard Drawn H80 (Regular)', lrg: 'Seamless Light Drawn H55 (Regular)', merge: false };
    }
    if (u.includes('SS') || u.includes('TP3') || u.includes('DSS') || u.includes('SDSS')) {
        return { sml: 'Seamless', lrg: 'Welded, 100% RT', merge: false };
    }
    return { sml: 'Seamless', lrg: 'LSAW, 100% RT', merge: false };
}

function _dsPipeEnds(material, service) {
    if (_isGreMat(material)) {
        return _isBonstrandService(service)
            ? { sml: 'Manufacturer standard (BONSTRAND Series 50000C)',
                lrg: 'Manufacturer standard (BONSTRAND Series 50000C)', merge: true }
            : { sml: 'Taper / Taper Socket x Spigot, Adhesive bonded',
                lrg: 'Taper / Taper Socket x Spigot, Adhesive bonded', merge: true };
    }
    if (_isCpvcMat(material)) {
        return { sml: 'Socket on one end', lrg: 'Socket on one end', merge: true };
    }
    if (_isCuNiMat(material)) {
        return { sml: 'PE', lrg: 'Bevel Ends', merge: false };
    }
    if (_isCopperMat(material)) {
        return { sml: 'BE', lrg: 'BE', merge: true };
    }
    return { sml: 'BE', lrg: 'BE', merge: false };
}

function _dsPipeMocMerge(material) {
    return _isCuNiMat(material) || _isCopperMat(material) || _isGreMat(material);
}

// Pipe MOC text for GRE — material spec for A50/A52, BONSTRAND placeholder for A51.
function _dsPipeMocGre(service, fs) {
    if (_isBonstrandService(service)) return 'Manufacturer standard (BONSTRAND Series 50000C)';
    return fs.pipe || '—';
}

// Pipe-data "Code" row label — defaults to ASME B 36.10M, but GRE uses the
// project's "Manufacturer's Std." designation (BONSTRAND for A51).
function _dsPipeCodeLabel(material, service) {
    if (_isGreMat(material)) {
        return _isBonstrandService(service)
            ? "Manufacturer's Std (BONSTRAND Series 50000C)"
            : "Manufacturer's Std.";
    }
    if (_isCpvcMat(material)) return 'ASTM F 441';
    return 'ASME B 36.10M';
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


// Connection style by material:
//   Galvanised CS  → Screwed (SCRD) #3000 small bore, BW large bore
//   90/10 CuNi     → SW small bore, BW Welded large bore (per EEMUA 234)
//   Everything else → BW Seamless small bore, BW Welded large bore
function _dsConnectionStyle(material, service) {
    if (_isTitaniumMat(material)) {
        const txt = 'Butt Weld (SCH to match pipe), Seamless';
        return { sm_type: txt, lg_type: txt, merge: true };
    }
    if (_isGreMat(material)) {
        const txt = _isBonstrandService(service)
            ? 'Manufacturer standard (BONSTRAND Series 50000C)'
            : 'Taper / Taper Socket x Spigot, Adhesive bonded';
        return { sm_type: txt, lg_type: txt, merge: true };
    }
    if (_isCuNiMat(material)) {
        return { sm_type: 'SW', lg_type: 'BW, Welded', merge: false };
    }
    if (_isCopperMat(material)) {
        return {
            sm_type: 'Brazed Fittings (SCH to match pipe), Seamless',
            lg_type: 'Butt Weld (SCH to match pipe), Seamless',
            merge: false,
        };
    }
    if (/GALV/i.test(material || '')) {
        return {
            sm_type: 'Screwed (SCRD), #3000',
            lg_type: 'Butt Weld (SCH to match pipe), Seamless',
            merge: false,
        };
    }
    return {
        sm_type: 'Butt Weld (SCH to match pipe), Seamless',
        lg_type: 'Butt Weld (SCH to match pipe), Welded',
        merge: false,
    };
}


// Component spec table — material-aware. SCRD families pull forged-fitting
// standards (B16.11) on small bore; BW families use B16.9 both bores. The
// MOC strings come straight from fitting_specs so adding a new material
// family in that file flows through automatically.
function _dsComponents(material, fs, service) {
    const u          = (material || '').toUpperCase();
    const isGalv     = /GALV/.test(u);
    const isCuNi     = _isCuNiMat(u);
    const isCopper   = _isCopperMat(u);
    const isGre      = _isGreMat(u);
    const isTi       = _isTitaniumMat(u);
    const fittingMoc = fs.fittings || '—';
    const flangeMoc  = fs.flange || '—';
    const branchMoc  = fs.branch_outlet || '—';

    if (isTi) {
        // Titanium (digit 70): BW Seamless, single column (small/large
        // bore not distinguished — Ti pipe per the project is one set).
        // Component list per project A70 sheet — adds Elbolet, Nipoflange,
        // Nipple. Elbow row carries the long B 16.9 + B 16.28 reference.
        return {
            sm_moc: fittingMoc, lg_moc: fittingMoc, moc_merge: true,
            rows: [
                { name: 'Elbow',      sm: 'ASME B 16.9 and ASME B 16.28 for short radius elbow and returns', merge: true },
                { name: 'Tee',        sm: 'ASME B 16.9',                                    merge: true },
                { name: 'Red.',       sm: 'ASME B 16.9',                                    merge: true },
                { name: 'Cap',        sm: 'ASME B 16.9',                                    merge: true },
                { name: 'Plug',       sm: 'Hex Head Plug, ASME B 16.11',                    merge: true },
                { name: 'Elbolet',    sm: 'MSS SP 97',                                      merge: true },
                { name: 'Weldolet',   sm: 'MSS SP 97',                                      merge: true },
                { name: 'Nipoflange', sm: 'ASTM B 363 Gr. WPT 2 (Ref. Section 1.24)',       merge: true },
                { name: 'Nipple',     sm: 'ASME B 36.10M, MOC Same as pipe',                merge: true },
            ],
        };
    }

    if (isGre) {
        // GRE classes (A50/A51/A52). Fittings have a special row list with
        // additional GRE-specific entries (Mold. Tee, Red. Sad, Reducer,
        // Coupler, Adaptor). A separate Rating row sits between TYPE and
        // MOC. Each value spans both bores (single column visually).
        const isBon = _isBonstrandService(service);
        const moc = isBon ? 'Manufacturer standard (BONSTRAND Series 50000C)' : fittingMoc;
        return {
            sm_moc: moc, lg_moc: moc, moc_merge: true,
            // GRE has an extra "Rating" row between TYPE and MOC.
            extra_rows: [
                { name: 'Rating', value: isBon ? 'Manufacturer standard (BONSTRAND Series 50000C)' : '20 bar, 93degC' },
            ],
            rows: [
                { name: 'Elbow',     sm: isBon ? moc : '22.5°, 45°, 90° elbow',            merge: true },
                { name: 'Tee',       sm: isBon ? moc : 'Tee or Reducing Tee',              merge: true },
                { name: 'Mold. Tee', sm: isBon ? moc : 'Molded Tee',                       merge: true },
                { name: 'Red. Sad',  sm: isBon ? moc : 'Reducing Saddle - Flat Face (FF)', merge: true },
                { name: 'Reducer',   sm: isBon ? moc : 'Conc and Ecc Reducer',             merge: true },
                { name: 'Coupler',   sm: isBon ? moc : 'Coupler',                          merge: true },
                { name: 'Adaptor',   sm: isBon ? moc : 'Adapter',                          merge: true },
            ],
        };
    }

    if (isCopper) {
        // Copper (digit 40): MOC values span vertically across the MOC row
        // AND every component row (B 124 forged on small bore, B 42 on
        // large bore — same value for every fitting type). Component rows
        // show labels only — the renderer skips their value cells.
        return {
            sm_moc: 'ASTM B 124 UNS C11000',
            lg_moc: 'ASTM B 42 UNS C12200',
            moc_rowspan: true,
            rows: [
                { name: 'Elbow' },
                { name: 'Tee' },
                { name: 'Red.' },
                { name: 'Cap' },
                { name: 'Coupl.' },
                { name: 'Plug' },
                { name: 'Union' },
                { name: 'Sockolet' },
                { name: 'Weldolet' },
                { name: 'Nipple' },
                { name: 'Swage' },
            ],
        };
    }

    if (isCuNi) {
        // CuNi (90/10) — per EEMUA 234 for every fitting. Same MOC text in
        // both bore columns (merged in the renderer); Nipple/Swage use the
        // pipe MOC. `merge: true` spans the cell across both bores.
        return {
            sm_moc: '90-10 Cu-Ni',
            lg_moc: '90-10 Cu-Ni',
            moc_merge: true,
            rows: [
                { name: 'Elbow',    sm: 'EEMUA 234', lg: 'EEMUA 234' },
                { name: 'Tee',      sm: 'EEMUA 234', lg: 'EEMUA 234' },
                { name: 'Red.',     sm: 'EEMUA 234', lg: 'EEMUA 234' },
                { name: 'Cap',      sm: 'EEMUA 234', lg: 'EEMUA 234' },
                { name: 'Coupl.',   sm: 'EEMUA 234', lg: '' },
                { name: 'Plug',     sm: 'EEMUA 234', lg: 'EEMUA 234' },
                { name: 'Union',    sm: 'EEMUA 234', lg: 'EEMUA 234' },
                { name: 'Sockolet', sm: 'EEMUA 234', lg: '' },
                { name: 'Weldolet', sm: 'EEMUA 234', lg: 'EEMUA 234' },
                { name: 'Nipple',   sm: 'EEMUA 234, MOC same as pipe', merge: true },
                { name: 'Swage',    sm: 'EEMUA 234, MOC same as pipe', merge: true },
            ],
        };
    }

    if (isGalv) {
        return {
            sm_moc: flangeMoc,
            lg_moc: fittingMoc,
            rows: [
                { name: 'Elbow',        sm: 'ASME B 16.11', lg: 'ASME B 16.9' },
                { name: 'Tee',          sm: 'ASME B 16.11', lg: 'ASME B 16.9' },
                { name: 'Red.',         sm: 'ASME B 16.11', lg: 'ASME B 16.9' },
                { name: 'Cap',          sm: 'ASME B 16.11', lg: 'ASME B 16.9' },
                { name: 'Coupl.',       sm: 'ASME B 16.11', lg: '' },
                { name: 'Hex Hd. Plug', sm: 'Hex Head Plug, ASME B 16.11', lg: '' },
                { name: 'Union',        sm: 'ASME B 16.11', lg: 'BS 3799' },
                { name: 'Olet',         sm: 'MSS SP-97',    lg: branchMoc },
                { name: 'Swage',        sm: `MSS SP-95, MOC same as pipe`, merge: true },
            ],
        };
    }
    return {
        sm_moc: fittingMoc,
        lg_moc: fittingMoc,
        rows: [
            { name: 'Elbow',     sm: 'ASME B 16.9',                lg: 'ASME B 16.9' },
            { name: 'Tee',       sm: 'ASME B 16.9',                lg: 'ASME B 16.9' },
            { name: 'Red.',      sm: 'ASME B 16.9',                lg: 'ASME B 16.9' },
            { name: 'Cap',       sm: 'ASME B 16.9',                lg: 'ASME B 16.9' },
            { name: 'Hex Hd. Plug', sm: 'Hex Head Plug, ASME B 16.11', lg: '' },
            { name: 'Weldolet',  sm: branchMoc,                    lg: branchMoc },
        ],
    };
}


// Flange TYPE per material:
//   90/10 CuNi → SW Flange (small bore) + WN Flange (large bore)
//   Galv CS    → Screwed (SCRD) + WN
//   Else       → WN both bores
function _dsFlangeType(material, service) {
    if (_isTitaniumMat(material)) {
        return { sm: 'Lap Joint Flange (Note 4) / WN Flange RF',
                 lg: 'Lap Joint Flange (Note 4) / WN Flange RF', merge: true };
    }
    if (_isGreMat(material)) {
        const txt = _isBonstrandService(service)
            ? 'Manufacturer standard (BONSTRAND Series 50000C)'
            : 'Taper / Taper Socket x Spigot, Adhesive bonded';
        return { sm: txt, lg: txt, merge: true };
    }
    if (_isCpvcMat(material)) {
        return { sm: '#150 Socket Type/ Manufacturer Standard',
                 lg: '#150 Socket Type/ Manufacturer Standard', merge: true };
    }
    if (_isCuNiMat(material)) {
        return { sm: 'SW Flange', lg: 'WN Flange', merge: false };
    }
    if (_isCopperMat(material)) {
        return { sm: 'Solid slip on flange', lg: 'Solid slip on flange', merge: true };
    }
    if (/GALV/i.test(material || '')) {
        return { sm: 'Screwed (SCRD)', lg: 'WN', merge: false };
    }
    return { sm: 'WN', lg: 'WN', merge: false };
}

// Flange "shape" — different per material family. Returns moc/std/face
// strings, whether to show the FACE row, and whether the section needs a
// trailing Blind Flange table.
function _dsFlangeBlock(material, fs, faceFull, service) {
    const flangeMoc = fs.flange || '—';
    if (_isTitaniumMat(material)) {
        return {
            moc:       'LJ=Inner Flange-B 363 WPT2, Outer Flange ASTM A 105N, Epoxy coated and WN= B 381 Gr. F2',
            std:       'ASME B 16.5, Butt Welding ends as per ASME B 16.25',
            show_face: false,           // Face info baked into TYPE row ("WN Flange RF")
            blind_moc: 'ASTM B 381 Gr. F 2 as per ASME B 16.5',
        };
    }
    if (_isCpvcMat(material)) {
        return {
            section_title: 'Flange (F 439, Bolt hole as per ASME B 16.5)',
            moc:           flangeMoc,
            std:           null,            // No separate STD row for CPVC (merged into title)
            show_face:     true,
            face:          'FF STOCK FINISH (1000 micro inch AARH)',
            blind_type:    '#150 / Manufacturer Standard',
            blind_moc:     flangeMoc,
            blind_face:    'FF STOCK FINISH (1000 micro inch AARH)',
        };
    }
    if (_isGreMat(material)) {
        const isBon = _isBonstrandService(service);
        return {
            moc:       isBon ? 'Manufacturer standard (BONSTRAND Series 50000C)' : flangeMoc,
            std:       isBon ? 'Drilled to ASME B 16.5, 150#' : 'Drilled to ASME B 16.5 / 16.47A, 150#',
            show_face: true,
            // BONSTRAND: face row also reads the manufacturer placeholder.
            face:      isBon ? 'Manufacturer standard (BONSTRAND Series 50000C)' : 'Flat Face (FF)',
            blind_moc: null,    // GRE doesn't have a separate Blind Flange entry
        };
    }
    if (_isCuNiMat(material)) {
        return {
            moc:      '90-10Cu-Ni',
            std:      'EEMUA 234 20 BAR',
            show_face: false,
            blind_moc: 'ASTM A 105N FF with 3mm 90-10 CuNi weld deposit',
        };
    }
    if (_isCopperMat(material)) {
        return {
            moc:      'ASTM B61 UNS C92200',
            std:      'ASME B 16.24',
            show_face: true,
            face:     'FF',
            blind_moc: 'ASTM A 105N RF With 3mm Copper over lay',
        };
    }
    return {
        moc:      flangeMoc,
        std:      'ASME B 16.5',
        show_face: true,
        face:     faceFull,
        blind_moc: null,
    };
}

// Section title for the bolts/nuts/gaskets block — Copper labels it
// "Mechanical Joints" per the project Excel template.
function _dsBoltsSectionTitle(material) {
    if (_isCopperMat(material) || _isCpvcMat(material)) return 'Mechanical Joints';
    return 'Bolts/ Nuts/ Gaskets';
}

function _dsPickSch(rows, lo, hi) {
    const counts = {};
    for (const r of rows) {
        const npsNum = parseFloat(r.nps);
        if (!r.sch_display || Number.isNaN(npsNum)) continue;
        // Project rule: NOT OK rows are excluded from the bore-mode pick.
        if (r.sch_status === 'NOT OK') continue;
        if (npsNum >= lo && npsNum <= hi) {
            counts[r.sch_display] = (counts[r.sch_display] || 0) + 1;
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
    const n = Number(v);
    // Drop trailing zeros — '125.0' → '125', '14.50' → '14.5'. Keeps the
    // P-T and pipe-data cells clean when values are integers (tubing) or
    // single-decimal (most ratings).
    return n.toFixed(dp).replace(/\.?0+$/, '');
}

// Tubing detection: class code starts with T followed by a digit (T80A,
// T90B, etc.), OR material name carries "Tubing" / 6 MO designation.
function _isTubingClass(classCode) {
    return /^T\d/i.test((classCode || '').trim());
}
function _isTubingMat(material) {
    return /Tubing|N08367|6\s*MO/i.test(material || '');
}


// Dedicated Datasheet for tubing classes — Pipe Data + Fittings Data +
// Valves only. No flange / spectacle / mechanical-joints sections.
function _renderTubingDatasheet(host, state, fs, v, pt) {
    const ptTemps  = pt.temperatures_c || [];
    const ptPress  = pt.pressures_barg || [];
    const ptLabels = pt.temp_labels || ptTemps.map(String);

    // Hydrotest comes from the P-T table when present (project tabulates
    // per-rating hydrotest); fall back to 1.5× max P if not stored.
    const groups = pt && pt.group ? null : null;
    let hydroBarg = pt && pt.hydrotest_barg;
    if (hydroBarg == null) {
        hydroBarg = ptPress.length ? Math.max(...ptPress) * 1.5 : 0;
    }

    const dims = window._npsDimensions;
    const tubingRows = (dims && dims.rows) || [];

    const sizeRow = tubingRows.map(r => `<td>${_dsFmtNps(r.nps_decimal)}</td>`).join('');
    const thkRow  = tubingRows.map(r => `<td>${r.wt_mm != null ? r.wt_mm : '—'}</td>`).join('');
    const ncols   = tubingRows.length;

    const pressCells = ptPress.map(p => `<td>${_dsFmt(p, 1)}</td>`).join('');
    const tempCells  = ptLabels.map(t => `<td>${escapeHtml(t)}</td>`).join('');

    // Pipe MOC text per material family.
    let pipeMoc = fs.pipe || '—';
    if (/6\s*MO/i.test(state.material)) {
        pipeMoc = 'ASTM A269 (UNS S31254) SML, Annealed, Hardness <= 90 HRB SML';
    } else if (/316L?/i.test(state.material) && /Tubing/i.test(state.material)) {
        pipeMoc = 'ASTM A269 Type 316/316L SML, Annealed, Hardness <= 90 HRB SML';
    }

    const fittingsMoc = 'Compression fitting with double ferrule, body AISI 316, ferrules and nuts in AISI 316';
    const fittingsEnds = 'OD X THD, OD X OD, & OD X SW (Manufacturer Standard)';

    const vCode = k => (v[k] || {}).code || '—';
    const cleanMat = (typeof cleanMaterial === 'function') ? cleanMaterial(state.material) : state.material;
    const displayedCode = _dsEffectiveClassCode(state) || state.classCode || '—';

    host.innerHTML = `
        <div class="ds-sheet">
            <!-- ── Header ── -->
            <table class="ds-table ds-header-tbl">
                <tr>
                    <td class="ds-logo-cell" rowspan="3"><img src="/static/images/logo.png" alt="Logo" onerror="this.style.display='none'"></td>
                    <td class="ds-header-title" colspan="4">PIPING MATERIAL SPECIFICATION</td>
                    <td class="ds-rev-lbl">Rev :</td>
                    <td class="ds-rev-val">A0</td>
                </tr>
                <tr class="ds-header-row">
                    <td class="ds-header-cell">Piping Class</td>
                    <td class="ds-header-cell">Material</td>
                    <td class="ds-header-cell">C.A</td>
                    <td class="ds-header-cell">Mill Tol</td>
                    <td class="ds-header-cell" colspan="2">Sheet No.</td>
                </tr>
                <tr>
                    <td class="ds-id-value"><strong>${escapeHtml(displayedCode)}</strong></td>
                    <td class="ds-id-value">—</td>
                    <td class="ds-id-value">${escapeHtml(cleanMat || '—')}</td>
                    <td class="ds-id-value">${escapeHtml(state.ca || '—')}</td>
                    <td class="ds-id-value">0.0%</td>
                    <td class="ds-id-value">${escapeHtml(displayedCode)}</td>
                </tr>
                <tr>
                    <td class="ds-label">Design Code:</td>
                    <td colspan="6" class="ds-value">—</td>
                </tr>
                <tr>
                    <td class="ds-label">Service:</td>
                    <td colspan="6" class="ds-value">${escapeHtml(state.service || '—')}</td>
                </tr>
                <tr>
                    <td class="ds-label">Branch Chart:</td>
                    <td colspan="6" class="ds-value">—</td>
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
                <tr class="ds-section-row"><td colspan="${ncols + 1}">Pipe Data</td></tr>
                <tr><td class="ds-label">Code</td><td colspan="${ncols}" class="ds-value">ASTM A 269</td></tr>
                <tr><td class="ds-label">Size (in)</td>${sizeRow}</tr>
                <tr><td class="ds-label">Sch. (Thk)</td>${thkRow}</tr>
                <tr><td class="ds-label">MOC</td><td colspan="${ncols}" class="ds-value">${escapeHtml(pipeMoc)}</td></tr>
                <tr><td class="ds-label">Ends</td><td colspan="${ncols}" class="ds-value">PE</td></tr>
                <tr><td class="ds-label">Fittings</td><td colspan="${ncols}" class="ds-value">According to manufacturer standard</td></tr>
            </table>

            <!-- ── Fittings Data (compression fitting sub-section) ── -->
            <table class="ds-table ds-pipe-tbl">
                <tr class="ds-section-row"><td colspan="${ncols + 1}">Fittings Data</td></tr>
                <tr><td class="ds-label">Size (in)</td>${sizeRow}</tr>
                <tr><td class="ds-label">TYPE</td><td colspan="${ncols}" class="ds-value">Compression Fitting</td></tr>
                <tr><td class="ds-label">MOC</td><td colspan="${ncols}" class="ds-value">${escapeHtml(fittingsMoc)}</td></tr>
                <tr><td class="ds-label">Ends</td><td colspan="${ncols}" class="ds-value">${escapeHtml(fittingsEnds)}</td></tr>
            </table>

            <!-- ── Valves ── -->
            <table class="ds-table">
                <tr class="ds-section-row"><td colspan="2">Valves</td></tr>
                <tr><td class="ds-label">Rating</td><td class="ds-value">${escapeHtml(v.rating || '—')}</td></tr>
                <tr><td class="ds-label">DBB (Inst)</td><td class="ds-value ds-code">${escapeHtml(vCode('dbb_inst'))}</td></tr>
                <tr><td class="ds-label">Needle (Inst)</td><td class="ds-value ds-code">${escapeHtml(vCode('needle'))}</td></tr>
                <tr><td class="ds-label">Ball (Inst)</td><td class="ds-value ds-code">${escapeHtml(vCode('ball'))}</td></tr>
                <tr><td class="ds-label">Check (Inst)</td><td class="ds-value ds-code">${escapeHtml(vCode('check'))}</td></tr>
            </table>
        </div>
    `;
}


function renderDatasheetTab(state, designPbarg, designTc) {
    const host = document.getElementById('rDatasheet');
    if (!host) return;
    const cf = state.codeFactors || {};
    const fs = cf.fitting_specs || {};
    const fx = cf.flange_extras || {};
    const v  = fx.valves || {};
    const pt = state.pt || {};

    // Tubing classes (T80A/B/C, T90A/B/C) have a fundamentally different
    // datasheet shape — no flange / blind flange / spectacle / bolts.
    // Route to a dedicated tubing renderer.
    if (_isTubingClass(state.classCode) || _isTubingMat(state.material)) {
        return _renderTubingDatasheet(host, state, fs, v, pt);
    }

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

    const svc       = state.service || '';
    const pipeType  = _dsPipeType(state.material, svc);
    const pipeEnds  = _dsPipeEnds(state.material, svc);
    const mergePipeMoc = _dsPipeMocMerge(state.material);
    let pipeMocSm = _dsPipeMoc(fs, state.material, false);
    let pipeMocLg = _dsPipeMoc(fs, state.material, true);

    // ── Pipe Data table cells (size / OD / Sch / WT for each NPS) ──
    const dsAxis = _dsNpsAxis();
    const npsCellsRow = dsAxis.map(n => `<td>${_dsFmtNps(n)}</td>`).join('');
    const odCellsRow  = dsAxis.map(n => {
        const r = rowsByNps[n];
        return `<td>${r ? _dsFmt(r.od_mm, 1) : '—'}</td>`;
    }).join('');
    const schCellsRow = dsAxis.map(n => {
        const r = rowsByNps[n];
        if (!r) return '<td>—</td>';
        // Project rule: NOT OK rows show '—' for SCH and echo calc_thk for WT.
        if (r.sch_status === 'NOT OK') return '<td>—</td>';
        return `<td>${r.sch_display ? escapeHtml(r.sch_display) : '—'}</td>`;
    }).join('');
    const wtCellsRow  = dsAxis.map(n => {
        const r = rowsByNps[n];
        if (!r) return '<td>—</td>';
        if (r.sch_status === 'NOT OK') {
            return `<td>${r.calc_thk_mm != null ? _dsFmt(r.calc_thk_mm, 2) : '—'}</td>`;
        }
        return `<td>${r.sel_thk_mm != null ? _dsFmt(r.sel_thk_mm, 2) : '—'}</td>`;
    }).join('');

    // GRE-specific rows: ID and WT come directly from the dimension file
    // (no schedule picking). Reads `id_mm` / `wt_mm` from the NPS dim
    // entries, not from the wt_rows the picker computes.
    const dims = window._npsDimensions;
    const dimByNps = {};
    if (dims && dims.rows) {
        for (const r of dims.rows) {
            const k = r.nps_decimal;
            if (typeof k === 'number') dimByNps[k] = r;
        }
    }
    const idCellsRow = dsAxis.map(n => {
        const r = dimByNps[n];
        return `<td>${r && r.id_mm != null ? _dsFmt(r.id_mm, 1) : '—'}</td>`;
    }).join('');
    const wtCellsRowGre = dsAxis.map(n => {
        const r = dimByNps[n];
        return `<td>${r && r.wt_mm != null ? _dsFmt(r.wt_mm, 1) : '—'}</td>`;
    }).join('');

    // Split TYPE / MOC / Ends across small + large bore columns.
    // Small bore = NPS ≤ 2, Large = NPS ≥ 2.5 — derived from the live axis
    // so adding NPS 36 / 42 / 48 to the data file just extends the large group.
    const smallCols = dsAxis.filter(n => n <= 2).length;
    const largeCols = dsAxis.length - smallCols;
    const typeRow = `
        <td colspan="${smallCols}">${escapeHtml(pipeType.sml)}</td>
        <td colspan="${largeCols}">${escapeHtml(pipeType.lrg)}</td>`;
    // CuNi pipe spec covers both bores in one line — merge the MOC cell.
    const mocRow = mergePipeMoc
        ? `<td colspan="${smallCols + largeCols}">${escapeHtml(pipeMocSm)}</td>`
        : `
            <td colspan="${smallCols}">${escapeHtml(pipeMocSm)}</td>
            <td colspan="${largeCols}">${escapeHtml(pipeMocLg)}</td>`;
    const endsRow = pipeEnds.merge
        ? `<td colspan="${smallCols + largeCols}">${escapeHtml(pipeEnds.sml)}</td>`
        : `<td colspan="${smallCols}">${escapeHtml(pipeEnds.sml)}</td>
           <td colspan="${largeCols}">${escapeHtml(pipeEnds.lrg)}</td>`;

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
    // GRE classes can have multiple gasket rows (EPDM Full Face + Flat Ring).
    const gasketSpecs = (fx.gasket || {}).specs || [(fx.gasket || {}).spec || '—'];
    const isGre = _isGreMat(state.material);
    const isBon = _isBonstrandService(svc);
    const pipeCodeLabel = _dsPipeCodeLabel(state.material, svc);
    if (isGre) {
        // Override the Pipe MOC text for GRE — use the project's long-form
        // pipe spec (or BONSTRAND placeholder for A51).
        pipeMocSm = _dsPipeMocGre(svc, fs);
        pipeMocLg = pipeMocSm;
    }

    // Spectacle
    const sp = fx.spectacle || {};

    // Flange
    const ratingNum  = state.rating || '—';
    const faceCode   = (fx.face || {}).code || '';
    const faceFull   = `${ratingNum} ${faceCode}, Serrated Finish`;
    const flangeTypeSplit = _dsFlangeType(state.material, svc);
    const flangeBlock = _dsFlangeBlock(state.material, fs, faceFull, svc);

    // Fittings — material-aware columns + standards
    const connStyle  = _dsConnectionStyle(state.material, svc);
    const components = _dsComponents(state.material, fs, svc);

    const cleanMat = (typeof cleanMaterial === 'function') ? cleanMaterial(state.material) : state.material;

    host.innerHTML = `
        <div class="ds-sheet">
            <!-- ── Header ── -->
            <table class="ds-table ds-header-tbl">
                <tr>
                    <td class="ds-logo-cell" rowspan="3"><img src="/static/images/logo.png" alt="Logo" onerror="this.style.display='none'"></td>
                    <td class="ds-header-title" colspan="4">PIPING MATERIAL SPECIFICATION</td>
                    <td class="ds-rev-lbl">Rev :</td>
                    <td class="ds-rev-val">${escapeHtml(_dsRevisionTag())}</td>
                </tr>
                <tr class="ds-header-row">
                    <td class="ds-header-cell">Piping Class</td>
                    <td class="ds-header-cell">Material</td>
                    <td class="ds-header-cell">C.A</td>
                    <td class="ds-header-cell">Mill Tol</td>
                    <td class="ds-header-cell" colspan="2">Sheet No.</td>
                </tr>
                <tr>
                    <td class="ds-id-value"><strong>${escapeHtml(_dsEffectiveClassCode(state) || '—')}</strong></td>
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
                    <td colspan="${ptTemps.length + 2}">${_isTitaniumMat(state.material) ? 'Pressure-Temperature Rating (EEMUA 234, Table 69)' : 'Pressure-Temperature Rating'}</td>
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

            <!-- ── Pipe Data ──
                 Unified layout — OD / SCH / SEL.THK come from the same
                 picker that drives Tab 3 (Wall Thickness Calculation),
                 so the two views always agree. Material-specific TYPE /
                 MOC / Ends / Code overrides still apply. GRE adds an
                 I.D. row from its dim file; CPVC adds a Fittings ref. -->
            <table class="ds-table ds-pipe-tbl">
                <tr class="ds-section-row">
                    <td colspan="${dsAxis.length + 1}">Pipe Data</td>
                </tr>
                <tr>
                    <td class="ds-label">Code</td>
                    <td colspan="${dsAxis.length}" class="ds-value">${escapeHtml(pipeCodeLabel)}</td>
                </tr>
                <tr>
                    <td class="ds-label">Size (in)</td>${npsCellsRow}
                </tr>
                <tr><td class="ds-label">O.D. mm</td>${odCellsRow}</tr>
                ${isGre ? `<tr><td class="ds-label">I.D. mm</td>${idCellsRow}</tr>` : ''}
                <tr><td class="ds-label">Sch.</td>${schCellsRow}</tr>
                <tr><td class="ds-label">WT. mm</td>${wtCellsRow}</tr>
                <tr>
                    <td class="ds-label">TYPE</td>${pipeType.merge
                        ? `<td colspan="${dsAxis.length}">${escapeHtml(pipeType.sml)}</td>`
                        : typeRow}
                </tr>
                <tr>
                    <td class="ds-label">MOC</td>${mocRow}
                </tr>
                <tr>
                    <td class="ds-label">Ends</td>${endsRow}
                </tr>
                ${_isCpvcMat(state.material)
                    ? `<tr><td class="ds-label">Fittings</td><td colspan="${dsAxis.length}" class="ds-value">ASTM F 439</td></tr>`
                    : ''}
            </table>

            <!-- ── Fittings Data ── -->
            ${_isCpvcMat(state.material) ? `
            <!-- CPVC: dedicated layout. Socket-type sub-section listing
                 standard components, then a Threaded sub-section (Union)
                 with its own TYPE/MOC pair. Single column — small/large
                 bore distinction doesn't apply to CPVC. -->
            <table class="ds-table">
                <tr class="ds-section-row"><td colspan="2">Fittings Data</td></tr>
                <tr><td class="ds-label">TYPE</td><td class="ds-value">Socket Type</td></tr>
                <tr><td class="ds-label">MOC</td><td class="ds-value"><strong>${escapeHtml(fs.fittings || '—')}</strong></td></tr>
                <tr><td class="ds-label">Elbow</td><td class="ds-value">${escapeHtml(fs.fittings || '—')}</td></tr>
                <tr><td class="ds-label">Tee</td><td class="ds-value">${escapeHtml(fs.fittings || '—')}</td></tr>
                <tr><td class="ds-label">Red.</td><td class="ds-value">${escapeHtml(fs.fittings || '—')}</td></tr>
                <tr><td class="ds-label">Cap</td><td class="ds-value">${escapeHtml(fs.fittings || '—')}</td></tr>
                <tr><td class="ds-label">Coupl.</td><td class="ds-value">${escapeHtml(fs.fittings || '—')}</td></tr>
                <tr><td class="ds-label">Union</td><td class="ds-value">ASTM F 437</td></tr>
                <tr><td class="ds-label">TYPE</td><td class="ds-value">Manufacturer Standard, Threaded (ASME B 1.20.1)</td></tr>
                <tr><td class="ds-label">MOC</td><td class="ds-value">${escapeHtml(fs.fittings || '—')}; O-ring material : EPDM</td></tr>
            </table>` : `
            <table class="ds-table">
                <tr class="ds-section-row"><td colspan="3">Fittings Data</td></tr>
                <tr>
                    <td class="ds-label">TYPE</td>
                    ${connStyle.merge
                        ? `<td colspan="2" class="ds-value">${escapeHtml(connStyle.sm_type)}</td>`
                        : `<td class="ds-value">${escapeHtml(connStyle.sm_type)}</td>
                           <td class="ds-value">${escapeHtml(connStyle.lg_type)}</td>`}
                </tr>
                ${(components.extra_rows || []).map(er => `
                    <tr>
                        <td class="ds-label">${escapeHtml(er.name)}</td>
                        <td colspan="2" class="ds-value">${escapeHtml(er.value || '—')}</td>
                    </tr>
                `).join('')}
                ${components.moc_rowspan
                    ? (() => {
                        // MOC cell spans MOC row + all component rows.
                        const span = components.rows.length + 1;
                        return `
                            <tr>
                                <td class="ds-label">MOC</td>
                                <td class="ds-value mocspan" rowspan="${span}"><strong>${escapeHtml(components.sm_moc)}</strong></td>
                                <td class="ds-value mocspan" rowspan="${span}"><strong>${escapeHtml(components.lg_moc)}</strong></td>
                            </tr>
                            ${components.rows.map(row => `
                                <tr><td class="ds-label">${escapeHtml(row.name)}</td></tr>
                            `).join('')}
                        `;
                    })()
                    : `<tr>
                            <td class="ds-label">MOC</td>
                            ${components.moc_merge
                                ? `<td colspan="2" class="ds-value"><strong>${escapeHtml(components.sm_moc)}</strong></td>`
                                : `<td class="ds-value"><strong>${escapeHtml(components.sm_moc)}</strong></td>
                                   <td class="ds-value"><strong>${escapeHtml(components.lg_moc)}</strong></td>`}
                        </tr>
                        ${components.rows.map(row => {
                            if (row.merge) {
                                return `<tr>
                                    <td class="ds-label">${escapeHtml(row.name)}</td>
                                    <td colspan="2" class="ds-value">${escapeHtml(row.sm || '—')}</td>
                                </tr>`;
                            }
                            return `<tr>
                                <td class="ds-label">${escapeHtml(row.name)}</td>
                                <td class="ds-value">${row.sm ? escapeHtml(row.sm) : '—'}</td>
                                <td class="ds-value">${row.lg ? escapeHtml(row.lg) : '—'}</td>
                            </tr>`;
                        }).join('')}`}
            </table>`}

            <!-- ── Flange ── -->
            <table class="ds-table">
                <tr class="ds-section-row"><td colspan="3">${escapeHtml(flangeBlock.section_title || 'Flange')}</td></tr>
                <tr>
                    <td class="ds-label">TYPE</td>
                    ${flangeTypeSplit.merge
                        ? `<td colspan="2" class="ds-value">${escapeHtml(flangeTypeSplit.sm)}</td>`
                        : `<td class="ds-value">${escapeHtml(flangeTypeSplit.sm)}</td>
                           <td class="ds-value">${escapeHtml(flangeTypeSplit.lg)}</td>`}
                </tr>
                <tr><td class="ds-label">MOC</td><td colspan="2" class="ds-value"><strong>${escapeHtml(flangeBlock.moc)}</strong></td></tr>
                ${flangeBlock.show_face
                    ? `<tr><td class="ds-label">FACE</td><td colspan="2" class="ds-value">${escapeHtml(flangeBlock.face)}</td></tr>`
                    : ''}
                ${flangeBlock.std
                    ? `<tr><td class="ds-label">STD</td><td colspan="2" class="ds-value">${escapeHtml(flangeBlock.std)}</td></tr>`
                    : ''}
            </table>

            ${flangeBlock.blind_moc ? `
            <!-- ── Blind Flange ── -->
            <table class="ds-table">
                <tr class="ds-section-row"><td colspan="2">Blind Flange</td></tr>
                ${flangeBlock.blind_type
                    ? `<tr><td class="ds-label">TYPE</td><td class="ds-value">${escapeHtml(flangeBlock.blind_type)}</td></tr>`
                    : ''}
                <tr><td class="ds-label">MOC</td><td class="ds-value">${escapeHtml(flangeBlock.blind_moc)}</td></tr>
                ${flangeBlock.blind_face
                    ? `<tr><td class="ds-label">FACE</td><td class="ds-value">${escapeHtml(flangeBlock.blind_face)}</td></tr>`
                    : ''}
            </table>` : ''}

            ${isGre ? `
            <!-- ── Spade and Spacer (GRE) ── -->
            <table class="ds-table">
                <tr class="ds-section-row"><td colspan="2">Spade and Spacer</td></tr>
                <tr><td class="ds-label">TYPE</td><td class="ds-value">Manufacturer standard, Flat Face (FF)</td></tr>
            </table>` : ((!_isCopperMat(state.material) && !_isCpvcMat(state.material) && !_isTitaniumMat(state.material)) ? `
            <!-- ── Spectacle Blind / Spacer Blinds (skipped for Copper) ── -->
            <table class="ds-table">
                <tr class="ds-section-row"><td colspan="3">Spectacle Blind/Spacer Blinds</td></tr>
                <tr><td class="ds-label">MOC</td><td colspan="2" class="ds-value"><strong>${escapeHtml(sp.moc || flangeMoc)}</strong></td></tr>
                <tr>
                    <td class="ds-label">Spectacle</td>
                    <td class="ds-value">${escapeHtml(sp.small_bore || '—')}</td>
                    <td class="ds-value">${escapeHtml(sp.large_bore || '—')}</td>
                </tr>
            </table>` : '')}

            <!-- ── Bolts / Nuts / Gaskets (Mechanical Joints for Copper) ── -->
            <table class="ds-table">
                <tr class="ds-section-row"><td colspan="2">${escapeHtml(_dsBoltsSectionTitle(state.material))}</td></tr>
                <tr><td class="ds-label">Stud Bolts</td><td class="ds-value">${escapeHtml(stud)}</td></tr>
                <tr><td class="ds-label">Hex Nuts</td><td class="ds-value">${escapeHtml(nut)}</td></tr>
                ${isGre ? `<tr><td class="ds-label">Washers</td><td class="ds-value">ASTM A 307 Gr. B HDG</td></tr>` : ''}
                ${gasketSpecs.map(g => `<tr><td class="ds-label">Gasket</td><td class="ds-value">${escapeHtml(g)}</td></tr>`).join('')}
            </table>

            ${(isGre && isBon) ? '' : `
            <!-- ── Valves (hidden for A51 BONSTRAND / Hypochlorite) ── -->
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
                ${v.needle ? `<tr><td class="ds-label">Needle</td><td class="ds-value ds-code">${escapeHtml(vCode('needle'))}</td></tr>` : ''}
            </table>`}

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
            const notOk       = r.sch_status === 'NOT OK';
            // Per project rule: when the standard schedule table cannot meet
            // the calc thk, the SCH cell is blanked and SEL.THK echoes the
            // calc thk rounded to 2 dp — engineer specs a custom wall.
            const schDisp     = notOk ? '—'
                              : (r.sch_display != null ? r.sch_display : blank);
            const selThkDisp  = notOk
                              ? (r.calc_thk_mm != null ? r.calc_thk_mm.toFixed(2) : blank)
                              : (r.sel_thk_mm != null ? r.sel_thk_mm.toFixed(2) : blank);
            const statusDisp  = r.sch_status != null ? r.sch_status : blank;
            const statusClass = r.sch_status === 'OK' ? 'wt-ok' : (notOk ? 'wt-alert' : '');

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
// the same state + code_factors as populateScheduleHeader. Picks a
// representative NPS as the worked example — preferentially NPS 6 for
// standard CS/SS, but falls back to the largest available NPS when the
// material's dimension table is shorter (e.g. Copper caps at NPS 4").
function renderFormulaCard(state, designPbarg, designTc) {
    const card = document.getElementById('rFormulaCard');
    if (!card) return;

    const dims = window._npsDimensions;
    if (!dims || !dims.rows || !dims.rows.length) {
        card.innerHTML = '<div class="formula-card-empty">Worked example unavailable — no NPS dimensions loaded.</div>';
        return;
    }
    // Prefer NPS 6 (B31.3 example convention); fall back to the largest
    // NPS in the loaded table so truncated material tables (Copper 0.5–4″)
    // still surface a worked example.
    const PREFERRED = [6.0, 4.0, 3.0, 2.0];
    let npsRow = null;
    for (const target of PREFERRED) {
        npsRow = dims.rows.find(r => r.nps_decimal === target);
        if (npsRow) break;
    }
    if (!npsRow) {
        // Last resort — biggest NPS in the table.
        npsRow = dims.rows.reduce((a, b) =>
            (b.nps_decimal > (a ? a.nps_decimal : -Infinity)) ? b : a, null);
    }
    if (!npsRow) {
        card.innerHTML = '<div class="formula-card-empty">Worked example unavailable.</div>';
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
    // Tolerance: 0.05 barg (= 0.7 psig) — within ASME B16.5 P-T table
    // rounding noise. Prevents false INADEQUATE when both values display
    // as the same rounded number (e.g. rating 14.46 vs typed 14.5).
    const adequate = ratedAtDesignT + 0.05 >= designPbarg;
    box.className = `adequacy-box ${adequate ? 'pass' : 'fail'}`;
    box.innerHTML = adequate
        ? `&#10003; Class ${escapeHtml(state.rating)} is <strong>ADEQUATE</strong>: ${fmt(ratedAtDesignT, 1)} barg &ge; Design ${fmt(designPbarg, 1)} barg at ${fmt(designTc, 0)}&deg;C`
        : `&#10005; Class ${escapeHtml(state.rating)} is <strong>INADEQUATE</strong>: rating ${fmt(ratedAtDesignT, 1)} barg &lt; Design ${fmt(designPbarg, 1)} barg at ${fmt(designTc, 0)}&deg;C`;
}

// Live recompute on Design P / T / Joint Type edits. The psig and MDMT
// inputs were removed per project request — psig is shown read-only in
// the Derived Conditions panel and MDMT stays at the project default
// (-29 °C) which is still carried through to the saved snapshot.
const _DEFAULT_MDMT_C = -29;

function wireReportInputs(state) {
    const pBarg = document.getElementById('rDesignPressure');
    const tC    = document.getElementById('rDesignTemperature');
    const tF    = document.getElementById('rTempFahrenheit');
    const joint = document.getElementById('rJointType');
    const jointRef = document.getElementById('rJointRef');

    // P-T curve data for two-way auto-sync between the Design P and
    // Design T inputs. Setting `element.value` from JS does NOT refire
    // the `input` event in the browser, so no infinite loop — but we
    // still gate with `syncing` defensively.
    const temps     = (state.pt && state.pt.temperatures_c) || [];
    const pressures = (state.pt && state.pt.pressures_barg) || [];
    const hasCurve = temps.length > 0 && pressures.length > 0;
    let syncing = false;

    // Format a pressure number for the input box — 1 decimal for whole
    // values (10.2 not 10.20), 2 decimals otherwise so 14.46 doesn't
    // round to 14.5 (which can falsely flag the next rating as inadequate).
    const fmtPressure = (p) =>
        (Math.abs(p * 10 - Math.round(p * 10)) < 1e-6) ? p.toFixed(1) : p.toFixed(2);
    const fmtTemp = (t) =>
        Number.isInteger(t) ? String(t) : t.toFixed(1);

    const refresh = () => {
        const dp = parseFloat(pBarg.value) || 0;
        const dt = parseFloat(tC.value) || 0;
        const md = _DEFAULT_MDMT_C;
        tF.textContent = `= ${fmt(cToF(dt), 1)} °F`;
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

    // ── Two-way auto-sync between Design P and Design T ───────────────
    // When the engineer edits Design T (°C), interpolate the curve's
    // rated P at that T and write it into Design P (barg). When they
    // edit Design P (barg), inverse-interpolate the T at which the
    // curve hits that P and write it into Design T (°C). Both writes
    // are silent (setting `.value` doesn't fire input), and we still
    // gate with `syncing` to be safe.

    tC.addEventListener('input', () => {
        if (syncing || !hasCurve) { refresh(); return; }
        const t = parseFloat(tC.value);
        if (Number.isFinite(t)) {
            const p = interpolatePressure(temps, pressures, t);
            if (p != null) {
                syncing = true;
                pBarg.value = fmtPressure(p);
                syncing = false;
            }
        }
        refresh();
    });

    pBarg.addEventListener('input', () => {
        if (syncing || !hasCurve) { refresh(); return; }
        const p = parseFloat(pBarg.value);
        if (Number.isFinite(p)) {
            const t = interpolateTemperature(temps, pressures, p);
            if (t != null) {
                syncing = true;
                tC.value = fmtTemp(t);
                syncing = false;
            }
        }
        refresh();
    });

    joint.addEventListener('input', refresh);
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
    // Use 2 dp for pressure so the auto-fill doesn't round 14.46 → 14.5,
    // which would then exceed the rating and falsely flag INADEQUATE.
    const pIn = document.getElementById('rDesignPressure');
    const tIn = document.getElementById('rDesignTemperature');
    if (pIn && !pIn.value) pIn.value = fmt(state.designP, 2);
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
        ensureNpsDimensions(state.material, state.service),
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
            // MDMT input was removed from the form — always send the
            // project default. The saved-PMS payload + Excel still carry
            // the field; to override per-class, extend the form here.
            const md = _DEFAULT_MDMT_C;
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
                        mdmt_c: md,
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

// Inverse of interpolatePressure — given a target rating pressure, return
// the temperature at which the curve hits that pressure. ASME B16.5 P-T
// curves are monotonically non-increasing (P drops with T), so:
//   • target ≥ cold-end P → clamp to T[0]  (any P above max rating just
//     means "use the cold-end design point")
//   • target ≤ hot-end P  → clamp to T[last]  (engineer can run all the
//     way to the hottest indexed temperature)
//   • flat segment        → pick the hot end of that flat region
//                            (most lenient T that still meets the rating)
function interpolateTemperature(temps, pressures, targetP) {
    if (!temps.length || !pressures.length) return null;
    if (targetP >= pressures[0]) return temps[0];
    if (targetP <= pressures[pressures.length - 1]) return temps[temps.length - 1];
    for (let i = 0; i < pressures.length - 1; i++) {
        const p1 = pressures[i], p2 = pressures[i + 1];
        if (p1 >= targetP && targetP >= p2) {
            if (p1 === p2) return temps[i + 1];   // flat — hot end of segment
            const t1 = temps[i], t2 = temps[i + 1];
            return t1 + (t2 - t1) * (p1 - targetP) / (p1 - p2);
        }
    }
    return temps[temps.length - 1];
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

    // Apply the displayed-class-code rule (GRE service-based override).
    const displayedCode = _displayedClassCode(inputs.material, inputs.service, data.class_code);
    panel.innerHTML = `
        <div class="ds-class-code">${escapeHtml(displayedCode)}</div>
        <div class="ds-class-meta">Resolved §5.5 class</div>
        <div class="ds-class-pills">${pills}</div>
    `;

    // Also surface the class code in the top-nav center for orientation.
    const navStatus = document.getElementById('navStatus');
    if (navStatus) {
        navStatus.innerHTML = `<span class="nav-status-pill">${escapeHtml(displayedCode)}</span>`;
    }

    // Seed design-conditions defaults so showReport() can pre-fill if empty.
    //
    // Policy: default Design T = `min(hottest_point.T, 300 °C)` and
    // Design P = curve-interpolated rated P at that capped T. The
    // ASME B16.5 Group 1.1 / 2.3 / 2.8 curves now extend to 538 /
    // 450 / 400 °C, but defaulting that hot pulls rated P way down.
    // Capping at 300 °C keeps the seeded operating point near typical
    // process conditions; the engineer can still type any T up to the
    // curve's published max and the rest of the page recomputes.
    const pt = data.pressure_temperature || {};
    const _temps     = pt.temperatures_c || [];
    const _pressures = pt.pressures_barg || [];
    const _hotT = (pt.hottest_point && pt.hottest_point.temperature_c);
    const SEED_TEMP_CAP_C = 300;

    let designT, designP;
    if (typeof _hotT === 'number' && _temps.length && _pressures.length) {
        designT = Math.min(_hotT, SEED_TEMP_CAP_C);
        designP = interpolatePressure(_temps, _pressures, designT);
    } else {
        // No P-T curve (e.g. CuNi 150#) → fall back to cold-end P and 38 °C.
        designP = (pt.cold_point && pt.cold_point.pressure_barg) || (_pressures[0]) || 0;
        designT = (_temps[0]) || 38;
    }

    const state = {
        rating:      inputs.rating,
        material:    inputs.material,
        ca:          inputs.ca,
        service:     inputs.service || '',
        classCode:   data.class_code,
        pt:          data.pressure_temperature,
        codeFactors: data.code_factors || null,
        designP:     designP,
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
