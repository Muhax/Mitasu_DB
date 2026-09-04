"""
Import Mitasu lubricant catalog (Catalog.csv or Catalog.pdf) + product photos
into a SQLite DB.

Re-runnable: drops and rebuilds all tables on each run, so updating the catalog
is just "replace the file / photos and run this again".

Config via environment variables (all optional):
    MITASU_CSV         path to the catalog file      (default: ./Catalog.csv)
                        used only when no path is given on the command line.
    MITASU_PHOTOS_DIR  path to the photos folder    (default: ./photos)
    MITASU_DB          path to the SQLite DB file   (default: ./catalog.db)

Usage:
    python import_catalog.py                # uses MITASU_CSV / ./Catalog.csv
    python import_catalog.py Catalog.csv     # explicit CSV export
    python import_catalog.py Catalog.pdf     # explicit PDF export (needs `pdfplumber`)
"""
import csv
import os
import re
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.environ.get("MITASU_CSV", os.path.join(BASE_DIR, "Catalog.csv"))
PHOTOS_DIR = os.environ.get("MITASU_PHOTOS_DIR", os.path.join(BASE_DIR, "photos"))
DB_PATH = os.environ.get("MITASU_DB", os.path.join(BASE_DIR, "catalog.db"))

IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# --- Column indexes in the raw CSV (0-based) --------------------------------
CSV_COL = {
    "group": 0,       # "BRAND\nModel" on first model of a brand, else "Model"
    "chassis": 1,     # chassis / type code, e.g. UA-ACV30N  (identifies a fitment row)
    "year": 5,        # "2001.9" (year.month) or "2011"
    "drive": 6,       # 2WD / 4WD
    "equipment": 7,   # grade / trim (free text)
    "engine": 19,     # engine model, e.g. 2AZ-FE
    "displacement": 23,
    "oil_capacity": 27,
    "oil_premium": 29,
    "oil_classic": 30,
    "filter": 33,
    "trans_type": 35,  # AT / CVT / MT / DCT / AMT
    "trans_capacity": 38,
    "atf_premium": 41,
    "atf_classic": 44,
    "diff_front": 45,
    "transfer": 49,
    "diff_rear": 52,
}

# --- Column indexes for the PDF export -------------------------------------
# The PDF prints a single dense 17-column table per page (no blank spacer
# columns like the Excel/CSV export has), extracted with pdfplumber using its
# ruling-line table strategy. Same fitment layout as the CSV, just packed.
PDF_NUM_COLS = 17
PDF_COL = {
    "group": 0,
    "chassis": 0,     # group rows leave every other column blank
    "year": 1,
    "drive": 2,
    "equipment": 3,
    "engine": 4,
    "displacement": 5,
    "oil_capacity": 6,
    "oil_premium": 7,
    "oil_classic": 8,
    "filter": 9,
    "trans_type": 10,
    "trans_capacity": 11,
    "atf_premium": 12,
    "atf_classic": 13,
    "diff_front": 14,
    "transfer": 15,
    "diff_rear": 16,
}

# slot -> (category, line label) for the product-code columns
PRODUCT_SLOTS = {
    "oil_premium":   ("Engine Oil", "Premium"),
    "oil_classic":   ("Engine Oil", "Classic"),
    "filter":        ("Oil Filter", ""),
    "atf_premium":   ("Transaxle Fluid", "Premium"),
    "atf_classic":   ("Transaxle Fluid", "Classic"),
    "diff_front":    ("Gear Oil", "Front Diff"),
    "transfer":      ("Gear Oil", "Transfer Box"),
    "diff_rear":     ("Gear Oil", "Rear Diff"),
}
SLOT_ORDER = list(PRODUCT_SLOTS.keys())

# Values that appear in the header block, repeated every ~page in the export.
CSV_HEADER_MARKERS = {
    CSV_COL["chassis"]: {"MODEL"},
    CSV_COL["year"]: {"YEAR"},
    CSV_COL["drive"]: {"DRIVING WHEELS"},
    CSV_COL["equipment"]: {"EQUIPMENT"},
    CSV_COL["engine"]: {"MODEL", "ENGINE"},
    CSV_COL["displacement"]: {"DISPLACEMENT CC"},
    CSV_COL["oil_premium"]: {"PREMIUM", "OIL TYPE"},
    CSV_COL["filter"]: {"FILTER"},
    CSV_COL["trans_type"]: {"TYPE", "TRANSAXLE"},
    CSV_COL["atf_premium"]: {"PREMIUM", "FLUID TYPE"},
    CSV_COL["diff_front"]: {"CAPACITY L.", "FRONT  DIFF.", "GEAR OIL TYPE"},
}

# The PDF repeats a fixed 3-row header band (column titles, some of it
# upside-down/rotated text) at the top of every page's table.
PDF_HEADER_MARKERS = {
    PDF_COL["chassis"]: {"MODEL"},
    PDF_COL["diff_front"]: {"CAPACITY L."},
    PDF_COL["engine"]: {"LEDOM"},  # "MODEL" printed upside-down
}

# Phrases that only ever show up in legend / footnote rows, never in a model name.
NOTE_KEYWORDS = ("transaxle", "transax", "capacity", "dry fill", "special product",
                 "domestic market", "not for", "hypoid", "lube ", "lsd attached")

CODE_RE = re.compile(r"^(MJ|MC|MO)-{0,2}([A-Z]?\d{2,4}[A-Z]?)$")
VARIANT_RE = re.compile(r"(?<![\d.])(\d)\s*Ln?(?![A-Za-z])", re.I)


def cell(row, idx):
    return row[idx].strip() if idx < len(row) else ""


def norm_text(s):
    """Collapse internal newlines / repeated whitespace."""
    return re.sub(r"\s+", " ", s.replace("\n", " ")).strip()


def clean_code(value):
    """Return a normalized product code (e.g. 'MJ-326') if `value` is a clean
    single code, else None. Handles 'MJ326', 'MJ--313', 'MJ-M02', 'MC-111J'."""
    s = value.strip().upper().replace(" ", "")
    m = CODE_RE.match(s)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def split_codes(value):
    """A cell may hold two space-separated codes ('MJ-104 MJ-112').
    Returns a list of (raw_token, clean_code_or_None). If nothing looks like a
    code, returns a single (whole_value, None) so the raw text is still shown."""
    tokens = value.split()
    hits = [(t, clean_code(t)) for t in tokens]
    if any(c for _, c in hits):
        return hits
    return [(value.strip(), None)]


def is_header_row(row, header_markers):
    for idx, markers in header_markers.items():
        if cell(row, idx) in markers:
            return True
    return False


def is_blank(row):
    return not any(c.strip() for c in row)


def is_group_row(row, col):
    group_col = col["group"]
    if not cell(row, group_col):
        return False
    return not any(cell(row, i) for i in range(len(row)) if i != group_col)


# --------------------------------------------------------------------------- #
#  Parse rows (from either the CSV or the PDF) into fitment records           #
# --------------------------------------------------------------------------- #
def parse_rows(rows, col, header_markers):
    fitments = []
    brand = None
    model = None
    current = None
    skipped_headers = 0

    for row in rows:
        if is_blank(row):
            continue
        if is_header_row(row, header_markers):
            skipped_headers += 1
            current = None
            continue
        if is_group_row(row, col):
            txt = cell(row, col["group"])
            # Footnotes / legends sit in the same column as model names but are
            # NOT a brand/model boundary -- skip them without disturbing state.
            low = txt.lower()
            if (txt.startswith(("*", "(", "!")) or "№" in txt or " - " in txt
                    or len(txt) > 45 or any(k in low for k in NOTE_KEYWORDS)):
                continue
            if "\n" in txt:
                b, m = txt.split("\n", 1)
                brand, model = norm_text(b), norm_text(m)
            else:
                model = norm_text(txt)
            current = None
            continue

        chassis = cell(row, col["chassis"])
        if chassis:
            # New fitment header row
            year_raw = norm_text(cell(row, col["year"]))
            ym = re.match(r"(\d{4})(?:\.(\d{1,2}))?", year_raw)
            rec = {
                "brand": brand or "",
                "model": model or "",
                "chassis": norm_text(chassis),
                "year_raw": year_raw,
                "year_num": int(ym.group(1)) if ym else None,
                "year_month": int(ym.group(2)) if ym and ym.group(2) else None,
                "drive": norm_text(cell(row, col["drive"])),
                "equipment": norm_text(cell(row, col["equipment"])),
                "engine": norm_text(cell(row, col["engine"])),
                "displacement": norm_text(cell(row, col["displacement"])),
                "oil_capacity": norm_text(cell(row, col["oil_capacity"])),
                "trans_type": norm_text(cell(row, col["trans_type"])),
                "trans_capacity": norm_text(cell(row, col["trans_capacity"])),
                "products": [],   # list of (slot, position, raw, code)
            }
            for slot in SLOT_ORDER:
                val = cell(row, col[slot])
                if not val:
                    continue
                for pos, (raw, code) in enumerate(split_codes(val)):
                    rec["products"].append((slot, pos, raw, code))
            fitments.append(rec)
            current = rec
        else:
            # Continuation row: extra / alternative codes for the fitment above.
            # Only accept values that are clean codes (skips leaked capacities).
            if current is None:
                continue
            for slot in SLOT_ORDER:
                val = cell(row, col[slot])
                if not val:
                    continue
                for raw, code in split_codes(val):
                    if code is None:
                        continue
                    base = sum(1 for p in current["products"] if p[0] == slot)
                    current["products"].append((slot, base, raw, code))

    return fitments, skipped_headers


def parse_catalog_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    return parse_rows(rows, CSV_COL, CSV_HEADER_MARKERS)


def parse_catalog_pdf(path):
    """Extract the same fitment rows from the PDF export via pdfplumber's
    ruling-line table detection (the PDF has no text-layer table structure,
    only vector gridlines, so `vertical_strategy`/`horizontal_strategy` must
    be forced to "lines"). Each page yields one 17-column table matching
    `PDF_COL`; anything narrower (the intro pages' legend tables) is skipped."""
    try:
        import pdfplumber
    except ImportError:
        sys.exit(
            "ERROR: reading a PDF catalog requires the 'pdfplumber' package.\n"
            "       Install it with: python -m pip install -r requirements.txt"
        )

    table_settings = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
    rows = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables(table_settings):
                if not table or len(table[0]) != PDF_NUM_COLS:
                    continue
                for row in table:
                    rows.append([c or "" for c in row])
    return parse_rows(rows, PDF_COL, PDF_HEADER_MARKERS)


# --------------------------------------------------------------------------- #
#  Scan the photos folder                                                     #
# --------------------------------------------------------------------------- #
def scan_photos(photos_dir):
    """Return list of dicts: {code, variant, filename, rel_path}."""
    out = []
    if not os.path.isdir(photos_dir):
        print(f"  WARNING: photos dir not found: {photos_dir}")
        return out
    for name in sorted(os.listdir(photos_dir)):
        folder = os.path.join(photos_dir, name)
        if not os.path.isdir(folder):
            continue
        code = name.strip().upper()
        for fn in sorted(os.listdir(folder)):
            if not fn.lower().endswith(IMG_EXTS):
                continue
            m = VARIANT_RE.search(fn)
            variant = m.group(1) if m else ""
            rel_path = f"{name}/{fn}".replace("\\", "/")
            out.append({"code": code, "variant": variant,
                        "filename": fn, "rel_path": rel_path})
    return out


# --------------------------------------------------------------------------- #
#  Build the database                                                         #
# --------------------------------------------------------------------------- #
SCHEMA = """
DROP TABLE IF EXISTS fitments;
DROP TABLE IF EXISTS fitment_products;
DROP TABLE IF EXISTS photos;

CREATE TABLE fitments (
    id             INTEGER PRIMARY KEY,
    brand          TEXT,
    model          TEXT,
    chassis        TEXT,
    year_raw       TEXT,
    year_num       INTEGER,
    year_month     INTEGER,
    drive          TEXT,
    equipment      TEXT,
    engine         TEXT,
    displacement   TEXT,
    oil_capacity   TEXT,
    trans_type     TEXT,
    trans_capacity TEXT
);

CREATE TABLE fitment_products (
    fitment_id  INTEGER NOT NULL,
    slot        TEXT NOT NULL,
    category    TEXT NOT NULL,
    line        TEXT NOT NULL,
    position    INTEGER NOT NULL,
    raw_value   TEXT NOT NULL,
    code        TEXT
);

CREATE TABLE photos (
    code      TEXT NOT NULL,
    variant   TEXT NOT NULL,
    filename  TEXT NOT NULL,
    rel_path  TEXT NOT NULL
);

CREATE INDEX idx_fit_brand        ON fitments(brand);
CREATE INDEX idx_fit_brand_model  ON fitments(brand, model);
CREATE INDEX idx_fit_cascade      ON fitments(brand, model, year_raw, engine);
CREATE INDEX idx_fp_fitment       ON fitment_products(fitment_id);
CREATE INDEX idx_fp_code          ON fitment_products(code);
CREATE INDEX idx_photos_code      ON photos(code);
"""


def build_db(db_path, fitments, photos):
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)

    fit_cols = ("brand", "model", "chassis", "year_raw", "year_num", "year_month",
                "drive", "equipment", "engine", "displacement", "oil_capacity",
                "trans_type", "trans_capacity")
    prod_rows = []
    for rec in fitments:
        cur = con.execute(
            f"INSERT INTO fitments ({','.join(fit_cols)}) "
            f"VALUES ({','.join('?' * len(fit_cols))})",
            tuple(rec[c] for c in fit_cols),
        )
        fid = cur.lastrowid
        for slot, pos, raw, code in rec["products"]:
            category, line = PRODUCT_SLOTS[slot]
            prod_rows.append((fid, slot, category, line, pos, raw, code))

    con.executemany(
        "INSERT INTO fitment_products "
        "(fitment_id, slot, category, line, position, raw_value, code) "
        "VALUES (?,?,?,?,?,?,?)", prod_rows)

    con.executemany(
        "INSERT INTO photos (code, variant, filename, rel_path) VALUES (?,?,?,?)",
        [(p["code"], p["variant"], p["filename"], p["rel_path"]) for p in photos])

    con.commit()
    return con, len(prod_rows)


def main():
    if len(sys.argv) > 2:
        sys.exit("Usage: python import_catalog.py [Catalog.csv|Catalog.pdf]")
    catalog_path = sys.argv[1] if len(sys.argv) == 2 else CSV_PATH
    ext = os.path.splitext(catalog_path)[1].lower()
    if ext not in (".csv", ".pdf"):
        sys.exit(f"ERROR: unsupported catalog file type '{ext}' (expected .csv or .pdf)")

    print(f"Catalog: {catalog_path}")
    print(f"Photos : {PHOTOS_DIR}")
    print(f"DB     : {DB_PATH}")
    if not os.path.isfile(catalog_path):
        sys.exit(f"ERROR: catalog file not found at {catalog_path}")

    print("\nParsing catalog ...")
    if ext == ".csv":
        fitments, skipped = parse_catalog_csv(catalog_path)
    else:
        fitments, skipped = parse_catalog_pdf(catalog_path)
    print(f"  fitment rows      : {len(fitments)}")
    print(f"  header rows skipped: {skipped}")

    print("Scanning photos ...")
    photos = scan_photos(PHOTOS_DIR)
    print(f"  photo files       : {len(photos)}")

    print("Building database ...")
    con, n_products = build_db(DB_PATH, fitments, photos)

    # ---- summary / spot checks ------------------------------------------------
    q = con.execute
    brands = q("SELECT COUNT(DISTINCT brand) FROM fitments").fetchone()[0]
    models = q("SELECT COUNT(DISTINCT brand || '|' || model) FROM fitments").fetchone()[0]
    codes_used = q("SELECT COUNT(DISTINCT code) FROM fitment_products WHERE code IS NOT NULL").fetchone()[0]
    codes_with_photo = q(
        "SELECT COUNT(DISTINCT fp.code) FROM fitment_products fp "
        "JOIN photos p ON p.code = fp.code WHERE fp.code IS NOT NULL").fetchone()[0]
    variants = [r[0] for r in q(
        "SELECT DISTINCT variant FROM photos WHERE variant != '' ORDER BY variant").fetchall()]

    print("\n--- Summary --------------------------------------------------")
    print(f"  brands                 : {brands}")
    print(f"  models                 : {models}")
    print(f"  fitments               : {len(fitments)}")
    print(f"  product entries        : {n_products}")
    print(f"  distinct codes used    : {codes_used}")
    print(f"  ...of which have photos : {codes_with_photo}")
    print(f"  photo liter capacities  : {variants}")

    print("\n--- Spot check: DAIHATSU Altis / UA-ACV30N ------------------")
    for r in q("""SELECT id, year_raw, drive, equipment, engine FROM fitments
                  WHERE brand='DAIHATSU' AND model='Altis' AND chassis='UA-ACV30N'"""):
        print(f"  fitment {r[0]}: {r[1]} {r[2]} {r[4]} / {r[3]}")
        for p in q("""SELECT category, line, raw_value, code FROM fitment_products
                      WHERE fitment_id=? ORDER BY rowid""", (r[0],)):
            print(f"      {p[0]:16} {p[1]:12} raw={p[2]:12} code={p[3]}")

    print("\n--- Spot check: brands list --------------------------------")
    print("  " + ", ".join(sorted(r[0] for r in q("SELECT DISTINCT brand FROM fitments") if r[0])))

    con.close()
    print(f"\nDone. Wrote {DB_PATH}")


if __name__ == "__main__":
    main()
