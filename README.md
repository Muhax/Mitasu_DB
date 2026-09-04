# Mitasu Lubricant Lookup

A small local web app for looking up Mitasu product codes for a vehicle.
Pick **Car company → Type → Year → Model → Driving Wheels → Engine → Grade** and
the page shows the full recommended lubricant sheet for that vehicle — engine oil (Premium + Classic),
oil filter, transaxle fluid (Premium + Classic) and gear/diff oil — each with its
code and product photo.

---

## What's in the folder

| Path | What it is |
|------|------------|
| `Catalog.csv` / `Catalog.pdf` | Source catalog (Excel export, or the PDF export). **You replace this** when the catalog changes. |
| `photos/` | Product photos. One sub-folder per code (`photos/MJ-101/…`), any of `.jpg/.png/.webp`. **You add to this** when new photos arrive. |
| `import_catalog.py` | Reads a catalog file (CSV or PDF) + `photos/` and builds `catalog.db`. Re-runnable. |
| `app.py` | The local web server (Flask). |
| `static/index.html` | The single-page frontend. |
| `catalog.db` | Generated SQLite database. Not committed to git — rebuild it with the import script. |

---

## First-time setup

You need **Python 3.9+**.

```bash
# from this folder
python -m pip install -r requirements.txt
python import_catalog.py      # builds catalog.db
python app.py                 # starts the server
```

Then open <http://127.0.0.1:5000> in a browser.

---

## Updating the catalog or photos

1. **Replace the catalog file** with the new export — either `Catalog.csv` (Excel
   export, same column layout) or `Catalog.pdf` (the PDF export) — and/or
   **drop new image folders into `photos/`** — one folder per code, named
   exactly like the code in the catalog (`MJ-123`, `MC-110`, …). Image file
   names inside the folder don't matter; a `…1L…/…4Ln…/…5Ln…/…6Ln…` in the name
   is picked up as the liter capacity (1 L / 4 L / 5 L / 6 L) of the bottle in
   the photo.
2. **Re-run the import**, passing the file you want to load — this refreshes
   the data, it does not duplicate it:
   ```bash
   python import_catalog.py Catalog.csv
   # or
   python import_catalog.py Catalog.pdf
   ```
   Running it with no argument falls back to `MITASU_CSV` / `./Catalog.csv`.
   Reading a PDF needs the `pdfplumber` package (`pip install -r requirements.txt`)
   and takes a couple of minutes for a large catalog (630+ pages).
   It prints a summary (row counts, spot checks) so you can sanity-check the load.
3. **Restart `app.py`** if it was running.

---

## Running the site

```bash
python app.py
```

- Default address: <http://127.0.0.1:5000>
- The "Liter capacity" dropdown at the top switches every product photo between
  the 1 L / 4 L / 5 L / 6 L bottle shots (falls back to whatever exists
  for a given code).
- If several body/transmission variants match your selection, all of them are
  listed. If nothing matches, the page says so.

### Configuration (optional)

All settings are environment variables — nothing is hard-coded to this machine:

| Variable | Default | Purpose |
|----------|---------|---------|
| `PORT` | `5000` | Port to serve on |
| `HOST` | `127.0.0.1` | Bind address (`0.0.0.0` to expose on your LAN) |
| `MITASU_DB` | `./catalog.db` | SQLite database path |
| `MITASU_PHOTOS_DIR` | `./photos` | Photos folder path |
| `MITASU_CSV` | `./Catalog.csv` | Catalog CSV path (import script only) |

Example:

```bash
PORT=8080 HOST=0.0.0.0 python app.py
```

---

## Notes on the data

The catalog is an Excel export with merged cells, so the import script does some
cleanup:

- **Repeated header rows** (the "MODEL / YEAR / ENGINE …" band that reprints every
  page) and **legend/footnote rows** are skipped.
- **Brand** is carried down from each `BRAND` heading to the models under it.
- **Type** is the model-family heading in the catalog (e.g. `Terios`); **Model**
  is the chassis / type code from the catalog's `MODEL` column (e.g. `TA-J122E`,
  `TA-J102E`) and is shown exactly as printed.
- A fitment's **alternative codes** (extra codes stacked in the same Excel cell,
  which the export splits onto following rows) are collected and shown together.
- **Year** is kept as printed (`2001.9` = Sept 2001; some rows only have a year).
- The transaxle / gear-oil columns contain some non-code text (`SP` = special
  product, `N/D`, capacity figures, footnote marks like `MJ-411!`). These are
  shown **exactly as written**; a photo is shown only when the value is a clean
  `MJ-/MC-/MO-` code.
- Not every code has a photo folder yet — those tiles say "no photo on file".

When importing a **PDF** catalog, the same cleanup applies — the PDF has no
text-layer table structure, so the script reads it by its vector ruling lines
(via `pdfplumber`) instead of parsing the raw text. A handful of rows can be
lost if a page's gridlines are broken across a row (they get filtered out
along with the real footnotes); the printed `fitment rows` / `header rows
skipped` counts let you sanity-check the load either way.

Re-running `import_catalog.py` always rebuilds `catalog.db` from scratch, so
fixing the source file and re-importing is the way to correct anything.

---

## Hosting it later

The pieces are already deploy-friendly: relative paths, env-configurable DB /
photos / port, a standard WSGI app (`app:app`). To put it online you'd mainly:

- serve `app` under a real WSGI server (`gunicorn app:app`, `waitress-serve`, …),
- point `MITASU_PHOTOS_DIR` at wherever the photos live (or serve `photos/` and
  `static/` straight from nginx),
- ship `catalog.db` (or run the import as a build step).

No code changes needed for that — only configuration.
