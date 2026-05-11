# PMS Generator (new)

Step 1 starter — same look as the previous PMS Generator project, with the
four dropdown lists and the §5.5 class-naming rules sourced from the
reference Excel (`Pipe Class Sheets-With Tubing-updated.xlsx`) and stored
as JSON.

## Data files (single source of truth)

- `app/data/pressure_ratings.json` — dropdown list of pressure ratings
- `app/data/materials.json` — dropdown list of pipe materials
- `app/data/corrosion_allowances.json` — dropdown list of CAs
- `app/data/services.json` — services + `allow_custom: true` for the "Other (custom)" row
- `app/data/class_naming.json` — §5.5 rules: rating-letter table, material/CA → digit table, suffix rules (`L` for LTCS, `N` for NACE, auto-NACE for CS+6 mm)

Edit a JSON file, refresh the browser, the dropdown / resolver updates.
The Excel itself is **not** copied into this project.

## Class resolution

When the user picks Rating + Material + Corrosion Allowance, the form
calls `POST /api/resolve-class`, which derives the §5.5 class code (e.g.
`A1`, `F1LN`, `T80`) from the JSON rules alone — no catalogue lookup.
A new combination not in the original Excel still resolves correctly
(e.g. `1500# / CuNi / NIL → F30`).

## Run

```cmd
cd /d D:\targeticon\pms-generator-new
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
python run.py
```

Open <http://localhost:8004/>.

## AI engineering notes (optional)

Tab 5 generates context-aware engineering notes via Anthropic Claude. To enable it:

1. Create a `.env` file in the project root:

   ```env
   ANTHROPIC_API_KEY=sk-ant-api03-...
   ANTHROPIC_MODEL=claude-sonnet-4-6
   ```

2. Restart the server.

3. Click **Generate Notes** on Tab 5 — Claude reviews the resolved class +
   design conditions and surfaces 4-6 engineering considerations
   (NACE compliance, PWHT, NDE level, procurement, service-material
   compatibility).

The deterministic engineering layer (stress, schedules, wall thickness,
hydrotest) stays untouched — AI only augments human review. Without the
key, Tab 5 shows a setup hint and the rest of the app works normally.

## API

| Method | Path | Returns |
|--------|------|---------|
| `GET`  | `/api/options/all` | All four lists in one response — used by the form on load |
| `GET`  | `/api/options/pressure-ratings` | `{ ratings: [...] }` |
| `GET`  | `/api/options/materials` | `{ materials: [...] }` |
| `GET`  | `/api/options/corrosion-allowances` | `{ corrosion_allowances: [...] }` |
| `GET`  | `/api/options/services` | `{ services: [...], allow_custom: true }` |
| `POST` | `/api/resolve-class` | `{ class_code, letter, digit, suffix, note }` from `{rating, material, corrosion_allowance, service}` |
| `GET`  | `/health` | health probe |

## Re-extracting the lists

If the source Excel is updated, re-run the helper (it overwrites the four JSON files):

```cmd
python "D:\targeticon\pms-files\_extract_lists.py"
```
