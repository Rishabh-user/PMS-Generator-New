# PMS Generator (new)

Step 1 starter — same look as the previous PMS Generator project, with the
four dropdown lists sourced from the reference Excel
(`Pipe Class Sheets-With Tubing-updated.xlsx`) and stored as JSON.

## Lists (single source of truth)

- `app/data/pressure_ratings.json`
- `app/data/materials.json`
- `app/data/corrosion_allowances.json`
- `app/data/services.json` — `allow_custom: true` lights up the "Other (custom)" row in the picker

Edit a JSON file, refresh the browser, the dropdown updates. The Excel
itself is **not** copied into this project.

## Run

```cmd
cd /d D:\targeticon\pms-generator-new
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
python run.py
```

Open <http://localhost:8004/>.

## API

| Method | Path | Returns |
|--------|------|---------|
| `GET`  | `/api/options/all` | All four lists in one response — used by the form on load |
| `GET`  | `/api/options/pressure-ratings` | `{ ratings: [...] }` |
| `GET`  | `/api/options/materials` | `{ materials: [...] }` |
| `GET`  | `/api/options/corrosion-allowances` | `{ corrosion_allowances: [...] }` |
| `GET`  | `/api/options/services` | `{ services: [...], allow_custom: true }` |
| `GET`  | `/health` | health probe |

## Re-extracting the lists

If the source Excel is updated, re-run the helper (it overwrites the four JSON files):

```cmd
python "D:\targeticon\pms-files\_extract_lists.py"
```
