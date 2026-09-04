"""
Mitasu lubricant lookup - local web server.

Serves the single-page frontend, the product photos, and a small JSON API that
drives the cascading dropdowns
(Brand -> Type -> Year -> Model -> Driving Wheels -> Engine -> Grade)
and returns the full lubricant sheet for a selected vehicle.

"Type" is the model family printed as a heading in the catalog (e.g. "Terios");
"Model" is the chassis / type code from the catalog's MODEL column (e.g.
"TA-J122E").

Config via environment variables (all optional):
    MITASU_DB          SQLite DB built by import_catalog.py  (default: ./catalog.db)
    MITASU_PHOTOS_DIR  product photos folder                 (default: ./photos)
    HOST               bind address                          (default: 127.0.0.1)
    PORT               port                                  (default: 5000)

Run:
    python app.py
"""
import os
import sqlite3

from flask import Flask, abort, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MITASU_DB", os.path.join(BASE_DIR, "catalog.db"))
PHOTOS_DIR = os.environ.get("MITASU_PHOTOS_DIR", os.path.join(BASE_DIR, "photos"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5000"))

app = Flask(__name__, static_folder="static", static_url_path="/static")

# Cascade levels in order; each maps to the fitments column it draws from and
# the upstream params required before it can be queried.
#   type  = the model family printed as a heading in the CSV (e.g. "Terios")
#   model = the chassis / type code in the CSV's "MODEL" column (e.g. "TA-J122E")
LEVELS = {
    "brand":  ("brand",     []),
    "type":   ("model",     ["brand"]),
    "year":   ("year_raw",  ["brand", "type"]),
    "model":  ("chassis",   ["brand", "type", "year"]),
    "drive":  ("drive",     ["brand", "type", "year", "model"]),
    "engine": ("engine",    ["brand", "type", "year", "model", "drive"]),
    "grade":  ("equipment", ["brand", "type", "year", "model", "drive", "engine"]),
}
PARAM_COLUMN = {"brand": "brand", "type": "model", "year": "year_raw",
                "model": "chassis", "drive": "drive", "engine": "engine",
                "grade": "equipment"}

CATEGORY_ORDER = ["Engine Oil", "Oil Filter", "Transaxle Fluid", "Gear Oil"]
LINE_ORDER = ["Premium", "Classic", "", "Front Diff", "Transfer Box", "Rear Diff"]


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _filters(args, allowed):
    """Build a WHERE fragment + params from whichever allowed params are set."""
    clauses, params = [], []
    for name in allowed:
        val = args.get(name, "").strip()
        if val:
            clauses.append(f"{PARAM_COLUMN[name]} = ?")
            params.append(val)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/photos/<path:relpath>")
def photo(relpath):
    return send_from_directory(PHOTOS_DIR, relpath)


@app.route("/api/variants")
def variants():
    con = db()
    rows = con.execute(
        "SELECT DISTINCT variant FROM photos WHERE variant != '' ORDER BY variant"
    ).fetchall()
    con.close()
    return jsonify([r["variant"] for r in rows])


@app.route("/api/options")
def options():
    level = request.args.get("level", "")
    if level not in LEVELS:
        abort(400, f"unknown level: {level}")
    column, required = LEVELS[level]
    where, params = _filters(request.args, required)

    if level == "year":
        order = "ORDER BY MIN(year_num), MIN(year_month)"
        select = f"SELECT {column} AS v FROM fitments{where} GROUP BY {column} {order}"
    else:
        select = f"SELECT DISTINCT {column} AS v FROM fitments{where} ORDER BY {column}"
    con = db()
    rows = con.execute(select, params).fetchall()
    con.close()
    return jsonify([r["v"] for r in rows if r["v"]])


def _photos_for(con, code):
    if not code:
        return []
    rows = con.execute(
        "SELECT variant, rel_path FROM photos WHERE code = ? ORDER BY variant, filename",
        (code,)
    ).fetchall()
    return [{"variant": r["variant"], "url": f"/photos/{r['rel_path']}"} for r in rows]


def _build_sheet(con, fitment_id):
    prods = con.execute(
        "SELECT category, line, raw_value, code FROM fitment_products "
        "WHERE fitment_id = ? ORDER BY rowid", (fitment_id,)
    ).fetchall()

    grouped = {}   # (category, line) -> list of entries
    seen = set()
    for p in prods:
        key = (p["category"], p["line"])
        dedupe = (key, p["code"] or p["raw_value"].lower())
        if dedupe in seen:
            continue
        seen.add(dedupe)
        grouped.setdefault(key, []).append({
            "raw": p["raw_value"],
            "code": p["code"],
            "photos": _photos_for(con, p["code"]),
        })

    def sort_key(item):
        (cat, line), _ = item
        c = CATEGORY_ORDER.index(cat) if cat in CATEGORY_ORDER else len(CATEGORY_ORDER)
        l = LINE_ORDER.index(line) if line in LINE_ORDER else len(LINE_ORDER)
        return (c, l)

    return [
        {"category": cat, "line": line, "entries": entries}
        for (cat, line), entries in sorted(grouped.items(), key=sort_key)
    ]


@app.route("/api/result")
def result():
    required = ["brand", "type", "year", "model", "drive", "engine"]
    missing = [k for k in required if not request.args.get(k, "").strip()]
    if missing:
        return jsonify({"error": f"missing selection: {', '.join(missing)}",
                        "fitments": []}), 400

    where, params = _filters(request.args, required + ["grade"])
    con = db()
    fits = con.execute(
        "SELECT * FROM fitments" + where + " ORDER BY drive, equipment, trans_type",
        params
    ).fetchall()

    out = []
    for f in fits:
        out.append({
            "id": f["id"],
            "brand": f["brand"], "model": f["model"], "chassis": f["chassis"],
            "year_raw": f["year_raw"], "drive": f["drive"],
            "equipment": f["equipment"], "engine": f["engine"],
            "displacement": f["displacement"], "oil_capacity": f["oil_capacity"],
            "trans_type": f["trans_type"], "trans_capacity": f["trans_capacity"],
            "sheet": _build_sheet(con, f["id"]),
        })
    con.close()
    return jsonify({"fitments": out})


if __name__ == "__main__":
    if not os.path.isfile(DB_PATH):
        raise SystemExit(
            f"Database not found: {DB_PATH}\nRun  python import_catalog.py  first."
        )
    print(f"Mitasu lookup running at  http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=bool(os.environ.get("FLASK_DEBUG")))
