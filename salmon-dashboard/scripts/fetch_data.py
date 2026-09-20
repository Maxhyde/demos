#!/usr/bin/env python3
"""Fetch Norwegian government data on salmon farming and write JSON for the dashboard.

Sources (all public, licensed under NLOD unless noted):
  * Statistics Norway (SSB) table 03024   - weekly export price/volume of farmed salmon
  * SSB external trade by HS code/country  - monthly salmon export markets
  * SSB aquaculture annual sales           - annual salmon sales (quantity, value)
  * Fiskeridirektoratet biomass statistics - monthly standing stock/harvest/feed/losses per production area (Excel)
  * Fiskeridirektoratet Akvakulturregisteret (GIS) - licences, localities, holders, capacity (Mowi filter)
  * Fiskeridirektoratet escapes (GIS)      - reported escape incidents
  * BarentsWatch Fish Health API           - sea lice / disease per locality (needs free client credentials)
  * Norges Bank                            - EUR/NOK
  * config.json                            - traffic light decision (manually maintained, 2-year cadence)

The script never raises out of main(): every source is wrapped, its status is recorded in
data/latest.json["sources"], and a change list against the previous run is written to
data/changes.json.  Exit code is 0 even on partial failure; data/status.json tells the
workflow whether a critical source failed.
"""
from __future__ import annotations

import datetime as dt
import io
import itertools
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DEBUG = os.environ.get("DEBUG") == "1"
KEEP_RAW = os.environ.get("KEEP_RAW") == "1"
NOW = dt.datetime.now(dt.timezone.utc)
UA = "salmon-dashboard/1.0 (github.com/Maxhyde/demos; data for a public dashboard)"

SESSION = requests.Session()
SESSION.headers["User-Agent"] = UA
SESSION.headers["Accept"] = "application/json, */*;q=0.5"


# --------------------------------------------------------------------------- utils
def log(*a):
    print(*a, flush=True)


def dbg(*a):
    if DEBUG:
        print("[debug]", *a, flush=True)


def http(method: str, url: str, retries: int = 3, timeout: int = 90, **kw) -> requests.Response:
    last = None
    for attempt in range(retries):
        try:
            r = SESSION.request(method, url, timeout=timeout, **kw)
            if r.status_code in (429, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code} from {url}", response=r)
            r.raise_for_status()
            return r
        except requests.RequestException as e:  # noqa: PERF203
            last = e
            wait = 3 * (attempt + 1)
            log(f"  retry {attempt + 1}/{retries} for {url}: {e} (sleep {wait}s)")
            time.sleep(wait)
    raise last  # type: ignore[misc]


def get_json(url, **kw):
    return http("GET", url, **kw).json()


def fnum(v):
    """Coerce a cell value to float or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if s in ("", "-", "..", ":", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def pct(new, old):
    if new is None or old in (None, 0):
        return None
    return round((new - old) / old * 100.0, 1)


def r1(v, nd=1):
    return None if v is None else round(v, nd)


def iso_week_monday(year: int, week: int) -> str:
    return dt.date.fromisocalendar(year, week, 1).isoformat()


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def load_json(path: Path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def matches_company(text: str | None) -> bool:
    if not text:
        return False
    t = str(text).upper()
    return any(p.upper() in t for p in CONFIG["company"]["name_patterns"])


# --------------------------------------------------------------------------- SSB (PxWeb API v0)
SSB_BASE = "https://data.ssb.no/api/v0/en/table/"


def ssb_metadata(table: str) -> dict:
    return get_json(SSB_BASE + table)


def ssb_query(table: str, meta: dict, selections: dict | None = None, top_time: int = 300) -> dict:
    """Query a PxWeb table. Variables not in `selections` get all values; Tid gets the last N."""
    selections = selections or {}
    query = []
    for var in meta["variables"]:
        code = var["code"]
        if code in selections:
            query.append({"code": code, "selection": {"filter": "item", "values": list(selections[code])}})
        elif var.get("time") or code == "Tid":
            query.append({"code": code, "selection": {"filter": "top", "values": [str(top_time)]}})
        else:
            query.append({"code": code, "selection": {"filter": "all", "values": ["*"]}})
    body = {"query": query, "response": {"format": "json-stat2"}}
    dbg("SSB query", table, json.dumps(body)[:600])
    r = http("POST", SSB_BASE + table, json=body, timeout=120)
    return r.json()


def jsonstat_records(ds: dict) -> list[dict]:
    """Flatten a JSON-stat2 dataset into records: {dim: code, dim_label: label, value: v}."""
    ids = ds["id"]
    sizes = ds["size"]
    cats = []
    for did in ids:
        cat = ds["dimension"][did]["category"]
        index = cat.get("index")
        if isinstance(index, dict):
            codes = sorted(index, key=lambda k: index[k])
        elif isinstance(index, list):
            codes = index
        else:
            codes = list(cat.get("label", {}).keys())
        labels = cat.get("label", {})
        cats.append([(c, labels.get(c, c)) for c in codes])
    values = ds["value"]
    if isinstance(values, dict):  # sparse
        total = 1
        for s in sizes:
            total *= s
        dense = [None] * total
        for k, v in values.items():
            dense[int(k)] = v
        values = dense
    out = []
    for flat, combo in enumerate(itertools.product(*[range(s) for s in sizes])):
        rec = {"value": values[flat] if flat < len(values) else None}
        for i, (did, pos) in enumerate(zip(ids, combo)):
            code, label = cats[i][pos]
            rec[did] = code
            rec[did + "_label"] = label
        out.append(rec)
    return out


def fetch_ssb_weekly() -> dict:
    table = CONFIG["ssb"]["weekly_table"]
    meta = ssb_metadata(table)
    log("  SSB", table, meta.get("title"))
    for v in meta["variables"]:
        log(f"    var {v['code']} ({v['text']}): {len(v['values'])} values; first={v['values'][:3]} {v['valueTexts'][:3]} last={v['values'][-1]}")
    ds = ssb_query(table, meta, top_time=CONFIG["ssb"]["weeks_back"])
    recs = jsonstat_records(ds)
    other = [d for d in ds["id"] if d not in ("Tid", "ContentsCode")]
    by_week: dict[str, dict] = {}
    field_labels: dict[str, str] = {}
    for r in recs:
        wk = r["Tid"]
        row = by_week.setdefault(wk, {"week": wk})
        prod = " ".join(str(r[d + "_label"]) for d in other).lower()
        cont = str(r["ContentsCode_label"]).lower()
        if "fresh" in prod or "chilled" in prod or "fersk" in prod:
            kind = "fresh"
        elif "frozen" in prod or "fros" in prod:
            kind = "frozen"
        else:
            kind = re.sub(r"\W+", "_", prod).strip("_") or "total"
        if "price" in cont or "kilo" in cont or "pris" in cont:
            field = f"{kind}_nok_kg"
        else:
            field = f"{kind}_tonnes"
        row[field] = r["value"]
        field_labels[field] = f"{r[other[0] + '_label'] if other else ''} – {r['ContentsCode_label']}".strip(" –")
    series = sorted(by_week.values(), key=lambda x: x["week"])
    for row in series:
        m = re.match(r"(\d{4})U(\d{1,2})", row["week"])
        if m:
            row["year"], row["week_no"] = int(m[1]), int(m[2])
            try:
                row["week_start"] = iso_week_monday(row["year"], row["week_no"])
            except ValueError:
                row["week_start"] = None
        t, p = row.get("fresh_tonnes"), row.get("fresh_nok_kg")
        row["fresh_value_mnok"] = r1(t * p / 1000.0) if t is not None and p is not None else None
    # year-over-year references
    idx = {(r.get("year"), r.get("week_no")): r for r in series}
    for row in series:
        prev = idx.get((row.get("year", 0) - 1, row.get("week_no")))
        row["fresh_nok_kg_yoy_pct"] = pct(row.get("fresh_nok_kg"), prev.get("fresh_nok_kg")) if prev else None
        row["fresh_tonnes_yoy_pct"] = pct(row.get("fresh_tonnes"), prev.get("fresh_tonnes")) if prev else None
    return {
        "table": table,
        "title": meta.get("title"),
        "field_labels": field_labels,
        "units": {"tonnes": "tonnes (product weight)", "nok_kg": "NOK per kg, average export price"},
        "latest_week": series[-1]["week"] if series else None,
        "series": series,
    }


def _scale_from_label(label: str) -> tuple[float, str]:
    l = label.lower()
    if "tonn" in l:
        return 1e3, label
    if "1 000 000" in l or "million" in l or "mill." in l:
        return 1e6, label
    if "1 000" in l or "1,000" in l or "1000" in l or "thousand" in l:
        return 1e3, label
    return 1.0, label


def fetch_ssb_export_markets() -> dict:
    wanted = CONFIG["ssb"]["salmon_hs_codes"]
    errors = []
    for table in CONFIG["ssb"]["trade_tables"]:
        try:
            meta = ssb_metadata(table)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{table}: {e}")
            continue
        vars_ = {v["code"]: v for v in meta["variables"]}
        log(f"  SSB {table}: {meta.get('title')}; variables: " + ", ".join(f"{c}({v['text']},{len(v['values'])})" for c, v in vars_.items()))
        tid = vars_.get("Tid")
        if not tid or not re.match(r"\d{4}M\d{2}$", str(tid["values"][-1])):
            errors.append(f"{table}: not monthly (last Tid={tid['values'][-1] if tid else None})")
            continue
        var_code = next((c for c in vars_ if c.lower().startswith("varekode") or "commodity" in vars_[c]["text"].lower()), None)
        if not var_code:
            errors.append(f"{table}: no commodity variable")
            continue
        present = [c for c in wanted if c in set(vars_[var_code]["values"])]
        if not present:
            errors.append(f"{table}: none of {list(wanted)} present")
            continue
        selections = {var_code: present}
        for c, v in vars_.items():
            txt = v["text"].lower()
            if c.lower() == "impeks" or "import" in txt and "export" in txt:
                exp_vals = [val for val, t in zip(v["values"], v["valueTexts"]) if "export" in t.lower() or t.lower().startswith("eks")]
                if exp_vals:
                    selections[c] = exp_vals
        ds = ssb_query(table, meta, selections, top_time=CONFIG["ssb"]["months_back"])
        recs = jsonstat_records(ds)
        country_dim = next((d for d in ds["id"] if d.lower().startswith("land") or "country" in ds["dimension"][d].get("label", "").lower()), None)
        if not country_dim:
            errors.append(f"{table}: no country dimension in {ds['id']}")
            continue
        contents = ds["dimension"]["ContentsCode"]["category"]["label"]
        qty_code = next((c for c, l in contents.items() if "quantity" in l.lower() or "weight" in l.lower() or "mengde" in l.lower() or "kg" in l.lower()), None)
        val_code = next((c for c, l in contents.items() if "value" in l.lower() or "verdi" in l.lower() or "nok" in l.lower()), None)
        qty_scale, qty_label = _scale_from_label(contents.get(qty_code, "")) if qty_code else (1.0, "")
        val_scale, val_label = _scale_from_label(contents.get(val_code, "")) if val_code else (1.0, "")
        # aggregate: month -> country -> {fresh/frozen: tonnes, mnok}
        agg: dict[str, dict[str, dict]] = {}
        for r in recs:
            v = r["value"]
            if v is None:
                continue
            month = r["Tid"]
            country = r[country_dim + "_label"]
            ccode = r[country_dim]
            code = r[var_code]
            kind = "fresh" if code.startswith("0302") else "frozen"
            cell = agg.setdefault(month, {}).setdefault(country, {"code": ccode, "fresh_tonnes": 0.0, "frozen_tonnes": 0.0, "fresh_mnok": 0.0, "frozen_mnok": 0.0})
            if r["ContentsCode"] == qty_code:
                kg = v * qty_scale
                cell[f"{kind}_tonnes"] += kg / 1000.0
            elif r["ContentsCode"] == val_code:
                cell[f"{kind}_mnok"] += v * val_scale / 1e6
        months = sorted(agg)
        total_words = ("all countries", "total", "world", "alle land", "i alt")

        def is_total(name: str, code: str) -> bool:
            n = name.lower()
            return any(w in n for w in total_words) or code in ("0", "00", "999", "9999")

        monthly_total = []
        for m in months:
            ft = sum(c["fresh_tonnes"] for n, c in agg[m].items() if not is_total(n, c["code"]))
            fz = sum(c["frozen_tonnes"] for n, c in agg[m].items() if not is_total(n, c["code"]))
            fv = sum(c["fresh_mnok"] for n, c in agg[m].items() if not is_total(n, c["code"]))
            zv = sum(c["frozen_mnok"] for n, c in agg[m].items() if not is_total(n, c["code"]))
            monthly_total.append({"month": m, "fresh_tonnes": r1(ft), "frozen_tonnes": r1(fz), "fresh_mnok": r1(fv), "frozen_mnok": r1(zv),
                                  "total_tonnes": r1(ft + fz), "total_mnok": r1(fv + zv)})
        latest = months[-1] if months else None
        rows = []
        if latest:
            prev_year_month = f"{int(latest[:4]) - 1}{latest[4:]}"
            for name, c in agg[latest].items():
                if is_total(name, c["code"]):
                    continue
                tot_t = c["fresh_tonnes"] + c["frozen_tonnes"]
                tot_v = c["fresh_mnok"] + c["frozen_mnok"]
                if tot_t <= 0 and tot_v <= 0:
                    continue
                py = agg.get(prev_year_month, {}).get(name)
                py_t = (py["fresh_tonnes"] + py["frozen_tonnes"]) if py else None
                py_v = (py["fresh_mnok"] + py["frozen_mnok"]) if py else None
                rows.append({"country": name, "code": c["code"], "tonnes": r1(tot_t), "mnok": r1(tot_v),
                             "fresh_tonnes": r1(c["fresh_tonnes"]), "frozen_tonnes": r1(c["frozen_tonnes"]),
                             "nok_kg": r1(tot_v * 1e6 / (tot_t * 1000)) if tot_t > 0 else None,
                             "tonnes_yoy_pct": pct(tot_t, py_t), "mnok_yoy_pct": pct(tot_v, py_v)})
            rows.sort(key=lambda x: -(x["mnok"] or 0))
        return {"table": table, "title": meta.get("title"), "hs_codes": {c: wanted[c] for c in present},
                "quantity_label": qty_label, "value_label": val_label, "latest_month": latest,
                "monthly_total": monthly_total, "by_country_latest": rows[:25], "country_count": len(rows)}
    raise RuntimeError("; ".join(errors) or "no trade table worked")


def fetch_ssb_annual_sales() -> dict:
    table = CONFIG["ssb"]["annual_sales_table"]
    meta = ssb_metadata(table)
    vars_ = {v["code"]: v for v in meta["variables"]}
    log(f"  SSB {table}: {meta.get('title')}; variables: " + ", ".join(f"{c}({v['text']},{len(v['values'])})" for c, v in vars_.items()))
    selections = {}
    for c, v in vars_.items():
        if c in ("Tid", "ContentsCode"):
            continue
        sal = [val for val, t in zip(v["values"], v["valueTexts"]) if "salmon" in t.lower() or "laks" in t.lower()]
        if sal:
            selections[c] = sal[:3]
    ds = ssb_query(table, meta, selections, top_time=15)
    recs = jsonstat_records(ds)
    out: dict[str, dict] = {}
    for r in recs:
        row = out.setdefault(r["Tid"], {"year": r["Tid"]})
        label = str(r["ContentsCode_label"])
        key = re.sub(r"\W+", "_", label.lower()).strip("_")
        other = " / ".join(str(r[d + "_label"]) for d in ds["id"] if d not in ("Tid", "ContentsCode"))
        row.setdefault("labels", {})[key] = f"{other}: {label}".strip(": ")
        row[key] = (row.get(key) or 0) + (r["value"] or 0) if r["value"] is not None else row.get(key)
    series = sorted(out.values(), key=lambda x: x["year"])
    return {"table": table, "title": meta.get("title"), "selection": selections, "series": series}


# --------------------------------------------------------------------------- Fiskeridirektoratet biomass (Excel scrape)
def fetch_fdir_biomass() -> dict:
    import openpyxl  # noqa: PLC0415

    links: list[dict] = []
    page_used = None
    for page in CONFIG["fiskeridir"]["biomass_pages"]:
        try:
            r = http("GET", page, headers={"Accept": "text/html"})
        except Exception as e:  # noqa: BLE001
            log(f"  biomass page failed {page}: {e}")
            continue
        html = r.text
        page_used = r.url
        for m in re.finditer(r'<a[^>]+href="([^"]+\.xlsx?)(?:\?[^"]*)?"[^>]*>(.*?)</a>', html, flags=re.I | re.S):
            href, text = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if href.startswith("/"):
                href = "https://www.fiskeridir.no" + href
            links.append({"href": href, "text": re.sub(r"\s+", " ", text), "file": href.rsplit("/", 1)[-1]})
        if links:
            break
    log(f"  biomass page: {page_used}; {len(links)} xlsx links")
    for l in links:
        log(f"    - {l['file']}  [{l['text'][:80]}]")
    if not links:
        raise RuntimeError("no xlsx links found on biomass pages")

    this_year = NOW.year
    years_ok = {str(y) for y in range(this_year - CONFIG["fiskeridir"]["biomass_years_back"], this_year + 1)}
    chosen = []
    for l in links:
        yrs = set(re.findall(r"(20\d\d)", l["file"] + " " + l["text"]))
        if yrs & years_ok:
            chosen.append(l)
    # de-duplicate by file name
    seen = set()
    chosen = [c for c in chosen if not (c["file"] in seen or seen.add(c["file"]))]
    log(f"  downloading {len(chosen)} files for years {sorted(years_ok)}")
    files_meta = []
    tables: dict[str, list] = {}
    for l in chosen:
        try:
            r = http("GET", l["href"], headers={"Accept": "*/*"}, timeout=120)
        except Exception as e:  # noqa: BLE001
            log(f"    download failed {l['file']}: {e}")
            files_meta.append({**l, "error": str(e)})
            continue
        content = r.content
        if KEEP_RAW:
            (RAW / "biomass").mkdir(parents=True, exist_ok=True)
            (RAW / "biomass" / l["file"]).write_bytes(content)
        try:
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        except Exception as e:  # noqa: BLE001
            log(f"    not a workbook {l['file']}: {e}")
            files_meta.append({**l, "error": f"openpyxl: {e}"})
            continue
        sheets = []
        for ws in wb.worksheets:
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                rows.append(list(row))
                if i > 400:
                    break
            sheets.append({"name": ws.title, "rows": rows})
            # diagnostic dump so the parser can be written/maintained from CI logs
            log(f"    sheet '{ws.title}' ({len(rows)} rows read) of {l['file']}")
            for row in rows[:14]:
                cells = ["" if c is None else str(c)[:18] for c in row[:14]]
                log("       | " + " | ".join(cells))
        parsed = parse_biomass_workbook(l, sheets)
        for k, v in parsed.items():
            tables.setdefault(k, []).extend(v)
        files_meta.append({**l, "sheets": [s["name"] for s in sheets], "parsed_tables": {k: len(v) for k, v in parsed.items()}})
    return {"page": page_used, "files": files_meta, "tables": tables, "parser_version": PARSER_VERSION}


PARSER_VERSION = "0.1-dump-only"


def parse_biomass_workbook(link: dict, sheets: list[dict]) -> dict[str, list]:
    """Turn the Fiskeridirektoratet biomass workbooks into tidy rows.

    The first pipeline run only dumps the sheet layout to the log; the parser is
    filled in once the layout is known.  Returns {table_name: [rows]}.
    """
    return {}


# --------------------------------------------------------------------------- Fiskeridirektoratet GIS (ArcGIS REST)
def gis_json(url: str, **params) -> dict:
    params.setdefault("f", "json")
    j = get_json(url, params=params, timeout=120)
    if isinstance(j, dict) and "error" in j:
        raise RuntimeError(f"ArcGIS error from {url}: {j['error']}")
    return j


def gis_layer_info(layer_url: str) -> dict:
    info = gis_json(layer_url)
    log(f"  layer '{info.get('name')}' type={info.get('type')} geom={info.get('geometryType')} maxRecordCount={info.get('maxRecordCount')} oid={info.get('objectIdField')}")
    log("    fields: " + ", ".join(f"{f['name']}:{f['type'].replace('esriFieldType', '')}" for f in info.get("fields", [])))
    return info


def gis_query_all(layer_url: str, info: dict, where: str = "1=1", geometry: bool = True) -> list[dict]:
    oid = info.get("objectIdField") or "OBJECTID"
    page = max(100, min(int(info.get("maxRecordCount") or 1000), 2000))
    adv = info.get("advancedQueryCapabilities") or {}
    common = dict(where=where, outFields="*", returnGeometry="true" if geometry else "false", outSR=4326)
    feats: list[dict] = []
    if adv.get("supportsPagination", False):
        if adv.get("supportsOrderBy", False):
            common["orderByFields"] = oid
        offset = 0
        while True:
            j = gis_json(layer_url + "/query", resultOffset=offset, resultRecordCount=page, **common)
            fs = j.get("features", [])
            feats.extend(fs)
            if not fs or (not j.get("exceededTransferLimit", False) and len(fs) < page):
                break
            offset += len(fs)
            if offset > 200000:
                break
    else:
        ids = gis_json(layer_url + "/query", where=where, returnIdsOnly="true").get("objectIds") or []
        ids = sorted(ids)
        for i in range(0, len(ids), page):
            chunk = ids[i:i + page]
            j = gis_json(layer_url + "/query", objectIds=",".join(str(x) for x in chunk), **{k: v for k, v in common.items() if k != "where"})
            feats.extend(j.get("features", []))
    rows = []
    for f in feats:
        a = dict(f.get("attributes") or {})
        g = f.get("geometry") or {}
        if "x" in g and "y" in g and g["x"] is not None:
            a["lon"], a["lat"] = round(g["x"], 5), round(g["y"], 5)
        rows.append(a)
    log(f"  fetched {len(rows)} rows from {layer_url}")
    return rows


def epoch_ms_to_date(v):
    if v is None:
        return None
    try:
        return dt.datetime.fromtimestamp(int(v) / 1000, tz=dt.timezone.utc).date().isoformat()
    except (ValueError, OSError, TypeError):
        return None


def norm_keys(row: dict) -> dict:
    return {str(k).lower(): v for k, v in row.items()}


def pick(row: dict, *cands, default=None):
    """Pick the first present key (case-insensitive, substring fallback)."""
    low = {str(k).lower(): k for k in row}
    for c in cands:
        if c.lower() in low:
            return row[low[c.lower()]]
    for c in cands:
        for lk, k in low.items():
            if c.lower() in lk:
                return row[k]
    return default


def fetch_fdir_register() -> dict:
    layer = CONFIG["fiskeridir"]["register_layer"]
    info = gis_layer_info(layer)
    rows = gis_query_all(layer, info)
    if rows:
        log("  sample rows: " + json.dumps(rows[:2], ensure_ascii=False, default=str)[:1500])
    date_fields = {f["name"] for f in info.get("fields", []) if f["type"] == "esriFieldTypeDate"}
    for r in rows:
        for k in date_fields:
            if k in r:
                r[k] = epoch_ms_to_date(r[k])
    fields = [f["name"] for f in info.get("fields", [])]
    # classify
    recs = []
    for r in rows:
        rec = {
            "locality_no": pick(r, "loknr", "lok_nr", "lokalitetsnr", "localityno"),
            "locality_name": pick(r, "navn", "lok_navn", "lokalitetsnavn", "name"),
            "licence_no": pick(r, "tillatelsesnr", "till_nr", "tillatelse", "licence", "license"),
            "holder": pick(r, "innehaver", "till_innehaver", "holder", "selskap", "navn_innehaver", "org_navn"),
            "org_no": pick(r, "org_nr", "orgnr", "organisasjonsnr", "org"),
            "species": pick(r, "art", "species", "fiskeart"),
            "purpose": pick(r, "formaal", "formål", "purpose", "till_formaal"),
            "production_form": pick(r, "produksjonsform", "prod_form", "prodform"),
            "capacity": fnum(pick(r, "kapasitet", "till_kap", "capacity", "mtb")),
            "capacity_unit": pick(r, "enhet", "kap_enhet", "unit"),
            "municipality": pick(r, "kommune", "komm_navn", "municipality"),
            "county": pick(r, "fylke", "fylkesnavn", "county"),
            "production_area": pick(r, "prod_omr", "produksjonsomraade", "produksjonsområde", "prodomr", "po", "production_area"),
            "placement": pick(r, "plassering", "vann", "placement"),
            "status": pick(r, "status", "lok_status", "till_status"),
            "lat": r.get("lat"),
            "lon": r.get("lon"),
        }
        rec["is_company"] = matches_company(rec["holder"]) or str(rec["org_no"] or "") in set(CONFIG["company"]["org_numbers"])
        recs.append(rec)
    sp = lambda x: str(x or "").lower()  # noqa: E731
    salmonid = [r for r in recs if any(w in sp(r["species"]) for w in ("laks", "salmon", "ørret", "orret", "trout", "regnbue"))]
    commercial = [r for r in salmonid if "kommersiell" in sp(r["purpose"]) or "commercial" in sp(r["purpose"]) or not r["purpose"]]
    sea = [r for r in commercial if "matfisk" in sp(r["production_form"]) or "sjø" in sp(r["placement"]) or "sjo" in sp(r["placement"]) or not r["production_form"]]

    def summarize(sub: list[dict]) -> dict:
        locs = {r["locality_no"] for r in sub if r["locality_no"]}
        lics = {r["licence_no"] for r in sub if r["licence_no"]}
        cap_by_loc: dict = {}
        for r in sub:
            if r["locality_no"] and r["capacity"]:
                cap_by_loc[r["locality_no"]] = max(cap_by_loc.get(r["locality_no"], 0), r["capacity"])
        by_area: dict[str, dict] = {}
        for r in sub:
            pa = str(r["production_area"] or "?")
            pa = re.sub(r"\D", "", pa) or pa
            d = by_area.setdefault(pa, {"production_area": pa, "localities": set(), "licences": set(), "capacity_t": {}})
            if r["locality_no"]:
                d["localities"].add(r["locality_no"])
                if r["capacity"]:
                    d["capacity_t"][r["locality_no"]] = max(d["capacity_t"].get(r["locality_no"], 0), r["capacity"])
            if r["licence_no"]:
                d["licences"].add(r["licence_no"])
        areas = []
        for pa, d in sorted(by_area.items(), key=lambda kv: (len(kv[0]), kv[0])):
            areas.append({"production_area": pa, "name": CONFIG["production_areas"].get(pa, ""), "localities": len(d["localities"]),
                          "licences": len(d["licences"]), "locality_capacity_t": r1(sum(d["capacity_t"].values()), 0)})
        holders: dict[str, dict] = {}
        for r in sub:
            h = str(r["holder"] or "unknown")
            hd = holders.setdefault(h, {"holder": h, "org_no": r["org_no"], "localities": set(), "licences": set()})
            if r["locality_no"]:
                hd["localities"].add(r["locality_no"])
            if r["licence_no"]:
                hd["licences"].add(r["licence_no"])
        top_holders = sorted(({"holder": h["holder"], "org_no": h["org_no"], "localities": len(h["localities"]), "licences": len(h["licences"])}
                              for h in holders.values()), key=lambda x: -x["localities"])[:15]
        return {"rows": len(sub), "localities": len(locs), "licences": len(lics), "locality_capacity_t": r1(sum(cap_by_loc.values()), 0),
                "by_area": areas, "top_holders": top_holders}

    company_rows = [r for r in sea if r["is_company"]]
    company_all = [r for r in recs if r["is_company"]]
    entities = sorted({(str(r["holder"]), str(r["org_no"])) for r in company_all})
    log(f"  register: {len(recs)} rows, {len(sea)} salmonid commercial sea rows, company rows={len(company_rows)}, entities={entities}")
    company_locs: dict = {}
    for r in company_rows:
        if not r["locality_no"]:
            continue
        d = company_locs.setdefault(r["locality_no"], {**{k: r[k] for k in ("locality_no", "locality_name", "municipality", "county", "production_area", "lat", "lon", "capacity", "capacity_unit", "holder", "org_no")}, "licences": set(), "species": set()})
        if r["licence_no"]:
            d["licences"].add(str(r["licence_no"]))
        if r["species"]:
            d["species"].add(str(r["species"]))
        if r["capacity"] and (d["capacity"] or 0) < r["capacity"]:
            d["capacity"] = r["capacity"]
    company_localities = []
    for d in company_locs.values():
        d["licences"] = sorted(d["licences"])
        d["species"] = sorted(d["species"])
        d["production_area"] = re.sub(r"\D", "", str(d["production_area"] or "")) or d["production_area"]
        company_localities.append(d)
    company_localities.sort(key=lambda x: (str(x["production_area"]).zfill(2), str(x["locality_name"])))
    return {
        "layer": layer,
        "fields": fields,
        "row_count": len(recs),
        "industry_salmonid_sea": summarize(sea),
        "company": {
            "name": CONFIG["company"]["name"],
            "entities": [{"holder": h, "org_no": o} for h, o in entities],
            **summarize(company_rows),
            "localities_list": company_localities,
        },
    }


def fetch_fdir_escapes() -> dict:
    root = CONFIG["fiskeridir"]["gis_root"]
    folder = gis_json(root + "/Yggdrasil")
    names = [s["name"] for s in folder.get("services", [])]
    log("  Yggdrasil services: " + ", ".join(names))
    kws = [k.lower() for k in CONFIG["fiskeridir"]["escapes_keywords"]]
    cands = [s for s in folder.get("services", []) if any(k in s["name"].lower() for k in kws)]
    if not cands:
        # search all folders
        top = gis_json(root)
        for f in top.get("folders", []):
            try:
                sub = gis_json(f"{root}/{f}")
            except Exception:  # noqa: BLE001
                continue
            cands += [s for s in sub.get("services", []) if any(k in s["name"].lower() for k in kws)]
    if not cands:
        raise RuntimeError("no escape (rømming) service found in Yggdrasil folder")
    all_rows: list[dict] = []
    layers_used = []
    for s in cands:
        svc_url = f"{root}/{s['name']}/{s['type']}"
        try:
            svc = gis_json(svc_url)
        except Exception as e:  # noqa: BLE001
            log(f"  service failed {svc_url}: {e}")
            continue
        for lyr in svc.get("layers", []) + svc.get("tables", []):
            layer_url = f"{svc_url}/{lyr['id']}"
            log(f"  candidate layer {layer_url} '{lyr.get('name')}'")
            try:
                info = gis_layer_info(layer_url)
                if info.get("type") not in ("Feature Layer", "Table"):
                    continue
                rows = gis_query_all(layer_url, info, geometry=True)
            except Exception as e:  # noqa: BLE001
                log(f"    failed: {e}")
                continue
            date_fields = {f["name"] for f in info.get("fields", []) if f["type"] == "esriFieldTypeDate"}
            for r in rows:
                for k in date_fields:
                    if k in r:
                        r[k] = epoch_ms_to_date(r[k])
                r["_layer"] = lyr.get("name")
            if rows:
                log("  sample: " + json.dumps(rows[:2], ensure_ascii=False, default=str)[:1500])
            all_rows.extend(rows)
            layers_used.append({"url": layer_url, "name": lyr.get("name"), "rows": len(rows), "fields": [f["name"] for f in info.get("fields", [])]})
    incidents = []
    for r in all_rows:
        date = pick(r, "dato", "hendelsesdato", "meldt_dato", "date", "rapportert", "innmeldt")
        rec = {
            "date": date,
            "locality_no": pick(r, "loknr", "lok_nr", "lokalitetsnr", "lokalitet_nr"),
            "locality_name": pick(r, "lok_navn", "lokalitetsnavn", "lokalitet", "navn"),
            "company": pick(r, "selskap", "innehaver", "oppdretter", "firma", "company", "eier"),
            "species": pick(r, "art", "species"),
            "reported_count": fnum(pick(r, "antall_rapportert", "ant_rapportert", "rapportert_antall", "antall_romt", "antall_rømt", "antall")),
            "recaptured_count": fnum(pick(r, "gjenfanget", "antall_gjenfanget", "recaptured")),
            "avg_weight_kg": fnum(pick(r, "snittvekt", "vekt", "gjennomsnittsvekt")),
            "cause": pick(r, "aarsak", "årsak", "cause", "hendelse"),
            "status": pick(r, "status"),
            "municipality": pick(r, "kommune", "municipality"),
            "county": pick(r, "fylke", "county"),
            "production_area": pick(r, "prod_omr", "produksjonsomr", "po"),
            "lat": r.get("lat"),
            "lon": r.get("lon"),
            "id": pick(r, "objectid", "id", "hendelsesid", "saksnr", "meldingsnr"),
            "layer": r.get("_layer"),
        }
        rec["is_company"] = matches_company(rec["company"])
        incidents.append(rec)
    incidents.sort(key=lambda x: str(x["date"] or ""), reverse=True)
    cutoff = (NOW - dt.timedelta(days=730)).date().isoformat()
    recent = [i for i in incidents if str(i["date"] or "") >= cutoff]
    this_year = str(NOW.year)

    def ytd(rows, year):
        rows = [r for r in rows if str(r["date"] or "").startswith(year)]
        salmon = [r for r in rows if "laks" in str(r["species"] or "").lower() or "salmon" in str(r["species"] or "").lower() or not r["species"]]
        return {"incidents": len(rows), "salmon_incidents": len(salmon), "reported_fish": r1(sum(r["reported_count"] or 0 for r in salmon), 0),
                "company_incidents": sum(1 for r in rows if r["is_company"])}

    return {"layers": layers_used, "total_rows": len(incidents), "recent": recent[:400],
            "ytd": {this_year: ytd(incidents, this_year), str(NOW.year - 1): ytd(incidents, str(NOW.year - 1))},
            "company_recent": [i for i in recent if i["is_company"]]}


# --------------------------------------------------------------------------- BarentsWatch fish health
def bw_token() -> str | None:
    cid = os.environ.get("BARENTSWATCH_CLIENT_ID")
    sec = os.environ.get("BARENTSWATCH_CLIENT_SECRET")
    if not cid or not sec:
        return None
    r = http("POST", CONFIG["barentswatch"]["token_url"], data={"client_id": cid, "client_secret": sec, "scope": "api", "grant_type": "client_credentials"},
             headers={"Content-Type": "application/x-www-form-urlencoded"})
    return r.json()["access_token"]


class NotConfigured(Exception):
    pass


def fetch_barentswatch(register: dict | None, previous: dict | None) -> dict:
    token = bw_token()
    if not token:
        raise NotConfigured("BARENTSWATCH_CLIENT_ID / BARENTSWATCH_CLIENT_SECRET not set. Register a free client at https://www.barentswatch.no/minside/ and add the two repository secrets.")
    api = CONFIG["barentswatch"]["api_root"]
    hdr = {"Authorization": f"Bearer {token}"}
    loc_to_area: dict[str, str] = {}
    company_locs: set[str] = set()
    if register:
        for r in register.get("company", {}).get("localities_list", []):
            company_locs.add(str(r["locality_no"]))
    # production area mapping for all salmonid sea localities
    # (register summary only keeps the company's localities; we fetch the mapping from the register layer cheaply)
    try:
        layer = CONFIG["fiskeridir"]["register_layer"]
        info = gis_json(layer)
        pa_field = next((f["name"] for f in info.get("fields", []) if "prod" in f["name"].lower() and "om" in f["name"].lower()), None)
        lok_field = next((f["name"] for f in info.get("fields", []) if f["name"].lower() in ("loknr", "lok_nr", "lokalitetsnr")), None)
        if pa_field and lok_field:
            j = gis_json(layer + "/query", where="1=1", outFields=f"{lok_field},{pa_field}", returnGeometry="false", returnDistinctValues="true", resultRecordCount=20000)
            for f in j.get("features", []):
                a = f["attributes"]
                if a.get(lok_field) is not None and a.get(pa_field):
                    loc_to_area[str(a[lok_field])] = re.sub(r"\D", "", str(a[pa_field])) or str(a[pa_field])
    except Exception as e:  # noqa: BLE001
        log(f"  PA mapping failed: {e}")
    log(f"  locality->PA mapping: {len(loc_to_area)} localities; company localities: {len(company_locs)}")

    prev_weeks = {w["week"]: w for w in (previous or {}).get("weekly", [])} if previous else {}
    today = NOW.date()
    iso = today.isocalendar()
    n_back = CONFIG["barentswatch"]["weeks_backfill"] if not prev_weeks else CONFIG["barentswatch"]["weeks_refresh"]
    weeks = []
    for i in range(n_back):
        d = today - dt.timedelta(weeks=i + 1)
        y, w, _ = d.isocalendar()
        weeks.append((y, w))
    weekly = dict(prev_weeks)
    company_latest: list[dict] = []
    first_keys_logged = False
    for y, w in sorted(weeks):
        url = f"{api}/v1/geodata/fishhealth/locality/{y}/{w}"
        try:
            j = get_json(url, headers=hdr)
        except Exception as e:  # noqa: BLE001
            log(f"  BW week {y}-W{w} failed: {e}")
            continue
        locs = j.get("localities", j if isinstance(j, list) else [])
        if locs and not first_keys_logged:
            log(f"  BW locality keys: {sorted(locs[0].keys())}")
            first_keys_logged = True
        agg: dict[str, dict] = {}
        national = {"reporting": 0, "sum": 0.0, "over_limit": 0, "with_pd": 0, "with_ila": 0, "localities": 0}
        comp = []
        for l in locs:
            lno = str(l.get("localityNo") or l.get("localityNumber") or l.get("no") or "")
            pa = loc_to_area.get(lno, str(l.get("productionAreaId") or l.get("productionArea") or "?"))
            pa = re.sub(r"\D", "", pa) or "?"
            v = l.get("avgAdultFemaleLice")
            reported = bool(l.get("hasReportedLice"))
            limit = l.get("liceLimit") or CONFIG["lice"]["limit_default"]
            d = agg.setdefault(pa, {"production_area": pa, "localities": 0, "reporting": 0, "sum": 0.0, "over_limit": 0, "with_pd": 0, "with_ila": 0})
            d["localities"] += 1
            national["localities"] += 1
            if l.get("hasPd"):
                d["with_pd"] += 1
                national["with_pd"] += 1
            if l.get("hasIla"):
                d["with_ila"] += 1
                national["with_ila"] += 1
            if reported and v is not None:
                d["reporting"] += 1
                d["sum"] += float(v)
                national["reporting"] += 1
                national["sum"] += float(v)
                if float(v) > float(limit):
                    d["over_limit"] += 1
                    national["over_limit"] += 1
            if lno in company_locs:
                comp.append({"locality_no": lno, "name": l.get("name"), "production_area": pa, "avg_adult_female_lice": v, "reported": reported,
                             "lice_limit": limit, "over_limit": (v is not None and float(v) > float(limit)), "is_fallow": l.get("isFallow"),
                             "has_pd": l.get("hasPd"), "has_ila": l.get("hasIla"), "mechanical_removal": l.get("hasMechanicalRemoval"),
                             "substance_treatment": l.get("hasSubstanceTreatments"), "cleanerfish": l.get("hasCleanerfishDeployed"),
                             "lat": l.get("lat"), "lon": l.get("lon")})
        by_area = []
        for pa, d in sorted(agg.items(), key=lambda kv: (len(kv[0]), kv[0])):
            by_area.append({"production_area": pa, "localities": d["localities"], "reporting": d["reporting"],
                            "avg_adult_female_lice": r1(d["sum"] / d["reporting"], 3) if d["reporting"] else None,
                            "over_limit": d["over_limit"], "with_pd": d["with_pd"], "with_ila": d["with_ila"]})
        comp_rep = [c for c in comp if c["reported"] and c["avg_adult_female_lice"] is not None]
        weekly[f"{y}-W{w:02d}"] = {
            "week": f"{y}-W{w:02d}", "year": y, "week_no": w, "week_start": iso_week_monday(y, w),
            "national": {"localities": national["localities"], "reporting": national["reporting"],
                         "avg_adult_female_lice": r1(national["sum"] / national["reporting"], 3) if national["reporting"] else None,
                         "over_limit": national["over_limit"], "with_pd": national["with_pd"], "with_ila": national["with_ila"]},
            "company": {"localities": len(comp), "reporting": len(comp_rep),
                        "avg_adult_female_lice": r1(sum(c["avg_adult_female_lice"] for c in comp_rep) / len(comp_rep), 3) if comp_rep else None,
                        "over_limit": sum(1 for c in comp_rep if c["over_limit"]), "with_pd": sum(1 for c in comp if c["has_pd"]),
                        "with_ila": sum(1 for c in comp if c["has_ila"])},
            "by_area": by_area,
        }
        company_latest = comp
        time.sleep(0.3)
    weekly_list = sorted(weekly.values(), key=lambda x: x["week"])[-60:]
    return {"weeks_fetched": len(weeks), "latest_week": weekly_list[-1]["week"] if weekly_list else None,
            "weekly": weekly_list, "company_localities_latest": sorted(company_latest, key=lambda c: (-(c["avg_adult_female_lice"] or -1))),
            "limits": CONFIG["lice"]}


# --------------------------------------------------------------------------- Norges Bank
def fetch_norges_bank() -> dict:
    start = (NOW - dt.timedelta(days=365 * CONFIG["norges_bank"]["years_back"])).date().isoformat()
    j = get_json(CONFIG["norges_bank"]["series_url"], params={"format": "sdmx-json", "startPeriod": start, "locale": "en"})
    data = j.get("data", j)
    struct = data["structure"]
    obs_dims = struct["dimensions"]["observation"]
    time_vals = [v["id"] for v in obs_dims[0]["values"]]
    series = list(data["dataSets"][0]["series"].values())[0]["observations"]
    points = []
    for k, v in series.items():
        idx = int(k.split(":")[0])
        val = fnum(v[0]) if v else None
        if val is not None:
            points.append({"date": time_vals[idx], "eurnok": val})
    points.sort(key=lambda p: p["date"])
    # weekly averages (ISO week)
    wk: dict[str, list] = {}
    for p in points:
        y, w, _ = dt.date.fromisoformat(p["date"]).isocalendar()
        wk.setdefault(f"{y}-W{w:02d}", []).append(p["eurnok"])
    weekly = [{"week": k, "eurnok": r1(sum(v) / len(v), 4)} for k, v in sorted(wk.items())]
    return {"latest": points[-1] if points else None, "daily": points[-260:], "weekly": weekly}


# --------------------------------------------------------------------------- change detection
def compute_changes(latest: dict, previous: dict | None) -> list[dict]:
    items: list[dict] = []

    def add(category, title, detail="", severity="info", company=False, value=None, delta_pct=None):
        items.append({"category": category, "title": title, "detail": detail, "severity": severity,
                      "company": company, "value": value, "delta_pct": delta_pct})

    first_run = previous is None
    if first_run:
        add("system", "Baseline established", "First run: all figures below are the current published values. Week-over-week deltas start next run.")

    # weekly export
    cur = (latest.get("weekly_export") or {}).get("series") or []
    prev = (((previous or {}).get("weekly_export") or {}).get("series") or []) if previous else []
    prev_weeks = {r["week"]: r for r in prev}
    if cur:
        new_weeks = [r for r in cur if r["week"] not in prev_weeks] if prev else cur[-1:]
        for r in new_weeks[-3:]:
            i = cur.index(r)
            before = cur[i - 1] if i > 0 else None
            p, t = r.get("fresh_nok_kg"), r.get("fresh_tonnes")
            ww_p = pct(p, before.get("fresh_nok_kg")) if before else None
            ww_t = pct(t, before.get("fresh_tonnes")) if before else None
            sev = "notable" if ww_p is not None and abs(ww_p) >= 5 else "info"
            add("export", f"New week {r['year']} W{r['week_no']}: fresh salmon {p:.2f} NOK/kg" if p is not None else f"New week {r['week']}",
                f"Price {'%+.1f%% w/w' % ww_p if ww_p is not None else ''}{', %+.1f%% y/y' % r['fresh_nok_kg_yoy_pct'] if r.get('fresh_nok_kg_yoy_pct') is not None else ''}. "
                f"Volume {t:,.0f} t{' (%+.1f%% w/w' % ww_t if ww_t is not None else ''}{', %+.1f%% y/y)' % r['fresh_tonnes_yoy_pct'] if r.get('fresh_tonnes_yoy_pct') is not None else (')' if ww_t is not None else '')}." if t is not None else "",
                sev, value=p, delta_pct=ww_p)
        # revisions
        for r in cur[-8:]:
            o = prev_weeks.get(r["week"])
            if o and o.get("fresh_nok_kg") is not None and r.get("fresh_nok_kg") is not None:
                d = pct(r["fresh_nok_kg"], o["fresh_nok_kg"])
                if d is not None and abs(d) >= 0.5:
                    add("export", f"Revision: week {r['week']} fresh price {o['fresh_nok_kg']:.2f} → {r['fresh_nok_kg']:.2f} NOK/kg", f"{d:+.1f}% vs previously published", "info", delta_pct=d)

    # export markets
    em = latest.get("export_markets") or {}
    pem = (previous or {}).get("export_markets") or {}
    if em.get("latest_month") and em.get("latest_month") != pem.get("latest_month"):
        tot = em["monthly_total"][-1] if em.get("monthly_total") else {}
        top = em.get("by_country_latest") or []
        add("markets", f"New month {em['latest_month']}: salmon exports {tot.get('total_tonnes', 0):,.0f} t, {tot.get('total_mnok', 0):,.0f} MNOK",
            "Top markets: " + ", ".join(f"{c['country']} {c['tonnes']:,.0f} t ({c['tonnes_yoy_pct']:+.0f}% y/y)" if c.get("tonnes_yoy_pct") is not None else f"{c['country']} {c['tonnes']:,.0f} t" for c in top[:5]), "info")

    # biomass
    bm = latest.get("biomass") or {}
    pbm = (previous or {}).get("biomass") or {}
    if bm.get("latest_month") and bm.get("latest_month") != pbm.get("latest_month"):
        nat = bm.get("national_latest") or {}
        add("biomass", f"New biomass month {bm['latest_month']}",
            ", ".join(f"{k.replace('_', ' ')}: {v:,.0f}" for k, v in nat.items() if isinstance(v, (int, float))), "info")

    # register / company
    reg = latest.get("register") or {}
    preg = (previous or {}).get("register") or {}
    if reg.get("company"):
        c, pc = reg["company"], preg.get("company") or {}
        if not previous:
            add("company", f"{c['name']}: {c['localities']} sea localities, {c['licences']} licences in the register",
                f"Locality capacity {c['locality_capacity_t']:,.0f} t; entities: " + ", ".join(e['holder'] for e in c['entities']), "info", company=True)
        else:
            cur_l = {str(x["locality_no"]): x for x in c.get("localities_list", [])}
            old_l = {str(x["locality_no"]): x for x in pc.get("localities_list", [])}
            for k in sorted(set(cur_l) - set(old_l)):
                x = cur_l[k]
                add("company", f"New {c['name']} locality in register: {x['locality_name']} ({k})", f"PO{x['production_area']} {x.get('municipality') or ''}; capacity {x.get('capacity') or 0:,.0f} {x.get('capacity_unit') or ''}", "notable", company=True)
            for k in sorted(set(old_l) - set(cur_l)):
                x = old_l[k]
                add("company", f"{c['name']} locality removed from register: {x['locality_name']} ({k})", f"PO{x['production_area']}", "notable", company=True)
            if c.get("licences") != pc.get("licences"):
                add("company", f"{c['name']} licence count {pc.get('licences')} → {c.get('licences')}", "", "notable", company=True)
            if c.get("locality_capacity_t") != pc.get("locality_capacity_t") and pc.get("locality_capacity_t"):
                add("company", f"{c['name']} locality capacity {pc['locality_capacity_t']:,.0f} → {c['locality_capacity_t']:,.0f} t", "", "info", company=True,
                    delta_pct=pct(c["locality_capacity_t"], pc["locality_capacity_t"]))
        ind, pind = reg.get("industry_salmonid_sea") or {}, preg.get("industry_salmonid_sea") or {}
        if previous and ind and pind and ind.get("localities") != pind.get("localities"):
            add("industry", f"Industry sea localities in register {pind['localities']} → {ind['localities']}", "", "info")

    # escapes
    esc = latest.get("escapes") or {}
    pesc = (previous or {}).get("escapes") or {}
    if esc.get("recent"):
        old_ids = {(str(i.get("id")), str(i.get("date"))) for i in pesc.get("recent", [])}
        new = [i for i in esc["recent"] if (str(i.get("id")), str(i.get("date"))) not in old_ids] if previous else esc["recent"][:5]
        for i in new[:12]:
            add("escapes", f"{'New ' if previous else 'Recent '}escape report {i.get('date')}: {i.get('locality_name') or i.get('locality_no')} ({i.get('company') or 'company n/a'})",
                f"{i.get('species') or ''}; reported {i['reported_count']:,.0f} fish" if i.get("reported_count") is not None else str(i.get("species") or ""),
                "alert" if i.get("is_company") else "notable", company=bool(i.get("is_company")))

    # lice
    lice = latest.get("lice") or {}
    plice = (previous or {}).get("lice") or {}
    if lice.get("weekly"):
        w = lice["weekly"][-1]
        pw = lice["weekly"][-2] if len(lice["weekly"]) > 1 else None
        if not previous or w["week"] != (plice.get("latest_week")):
            n, c = w["national"], w["company"]
            add("lice", f"Lice week {w['week']}: national avg {n['avg_adult_female_lice']} adult female/fish, {n['over_limit']} of {n['reporting']} sites over limit",
                (f"Prev week {pw['national']['avg_adult_female_lice']}. " if pw else "") + f"{CONFIG['company']['name']}: avg {c['avg_adult_female_lice']}, {c['over_limit']} of {c['reporting']} reporting sites over limit; PD {c['with_pd']}, ILA {c['with_ila']}.",
                "alert" if c.get("over_limit") else "info", company=True, value=n["avg_adult_female_lice"])

    # fx
    fx = latest.get("fx") or {}
    pfx = (previous or {}).get("fx") or {}
    if fx.get("latest") and (not previous or fx["latest"].get("date") != (pfx.get("latest") or {}).get("date")):
        w = fx.get("weekly") or []
        d = pct(w[-1]["eurnok"], w[-2]["eurnok"]) if len(w) > 1 else None
        add("fx", f"EUR/NOK {fx['latest']['eurnok']:.3f} ({fx['latest']['date']})", f"Weekly average {w[-1]['eurnok']:.3f}, {d:+.1f}% w/w" if d is not None and w else "", "info", value=fx["latest"]["eurnok"], delta_pct=d)

    # traffic lights
    tl = latest.get("traffic_lights") or {}
    ptl = (previous or {}).get("traffic_lights") or {}
    if tl.get("decision_date") != ptl.get("decision_date"):
        reds = [f"PO{k}" for k, v in tl.get("colours", {}).items() if v == "red"]
        greens = [f"PO{k}" for k, v in tl.get("colours", {}).items() if v == "green"]
        add("regulation", f"Traffic light decision {tl.get('decision_date')}: green {', '.join(greens)}; red {', '.join(reds)}",
            f"{tl.get('green_growth_pct')}% growth offered in green areas, {tl.get('red_reduction_pct')}% in red. Next decision expected {tl.get('next_decision_expected')}.", "notable")

    # source health
    for key, s in (latest.get("sources") or {}).items():
        ps = ((previous or {}).get("sources") or {}).get(key) or {}
        if s.get("status") == "error" and ps.get("status") != "error":
            add("system", f"Source failed: {s.get('name', key)}", str(s.get("error"))[:300], "notable")
        elif s.get("status") == "ok" and ps.get("status") == "error":
            add("system", f"Source recovered: {s.get('name', key)}", "", "info")
    return items


# --------------------------------------------------------------------------- main
def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    previous = load_json(DATA / "latest.json")
    latest: dict = {"generated_at": NOW.isoformat(timespec="seconds"), "run_date": NOW.date().isoformat(),
                    "company": CONFIG["company"]["name"], "production_areas": CONFIG["production_areas"],
                    "traffic_lights": CONFIG["traffic_lights"], "lice_limits": CONFIG["lice"], "sources": {}}
    src_meta = CONFIG["sources"]

    def run(key: str, fn, *args, critical=False):
        meta = dict(src_meta.get(key, {"name": key}))
        log(f"\n=== {meta.get('name', key)} ===")
        t0 = time.time()
        try:
            result = fn(*args)
            meta.update(status="ok", fetched_at=NOW.isoformat(timespec="seconds"), seconds=round(time.time() - t0, 1), critical=critical)
            log(f"  ok in {meta['seconds']}s")
            latest["sources"][key] = meta
            return result
        except NotConfigured as e:
            meta.update(status="not_configured", error=str(e), critical=critical)
            log(f"  not configured: {e}")
        except Exception as e:  # noqa: BLE001
            meta.update(status="error", error=f"{type(e).__name__}: {e}"[:500], critical=critical)
            log(f"  FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()
        latest["sources"][key] = meta
        # keep previous data for a failed source so the dashboard does not go blank
        return None

    def keep_or(new, key):
        if new is not None:
            return new
        old = (previous or {}).get(key)
        if old is not None:
            latest["sources"].setdefault(key, {})
            latest["sources"][key]["stale_from_previous_run"] = (previous or {}).get("generated_at")
        return old

    latest["weekly_export"] = keep_or(run("ssb_weekly", fetch_ssb_weekly, critical=True), "weekly_export")
    latest["export_markets"] = keep_or(run("ssb_markets", fetch_ssb_export_markets), "export_markets")
    latest["annual_sales"] = keep_or(run("ssb_annual", fetch_ssb_annual_sales), "annual_sales")
    latest["biomass"] = keep_or(run("fdir_biomass", fetch_fdir_biomass), "biomass")
    latest["register"] = keep_or(run("fdir_register", fetch_fdir_register), "register")
    latest["escapes"] = keep_or(run("fdir_escapes", fetch_fdir_escapes), "escapes")
    latest["lice"] = keep_or(run("barentswatch_lice", fetch_barentswatch, latest.get("register"), (previous or {}).get("lice")), "lice")
    latest["fx"] = keep_or(run("norges_bank_fx", fetch_norges_bank), "fx")
    latest["sources"]["traffic_lights"] = {**src_meta["traffic_lights"], "status": "ok", "fetched_at": CONFIG["traffic_lights"]["decision_date"], "note": "Maintained manually in config.json (decisions every second year)."}

    changes = compute_changes(latest, previous)
    latest["change_count"] = len(changes)
    save_json(DATA / "latest.json", latest)
    save_json(DATA / "changes.json", {"generated_at": latest["generated_at"], "compared_to": (previous or {}).get("generated_at"), "items": changes})
    changelog = load_json(DATA / "changelog.json") or []
    changelog = [c for c in changelog if c.get("run_date") != latest["run_date"]]
    changelog.append({"run_date": latest["run_date"], "generated_at": latest["generated_at"], "items": changes,
                      "sources_ok": sum(1 for s in latest["sources"].values() if s.get("status") == "ok"),
                      "sources_failed": [k for k, s in latest["sources"].items() if s.get("status") == "error"]})
    save_json(DATA / "changelog.json", changelog[-30:])
    critical_failed = [k for k, s in latest["sources"].items() if s.get("status") == "error" and s.get("critical")]
    save_json(DATA / "status.json", {"generated_at": latest["generated_at"], "critical_failed": critical_failed,
                                     "statuses": {k: s.get("status") for k, s in latest["sources"].items()}})
    log("\n=== summary ===")
    for k, s in latest["sources"].items():
        log(f"  {k:18s} {s.get('status'):15s} {s.get('error', '')[:160] if s.get('error') else ''}")
    log(f"  changes: {len(changes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
