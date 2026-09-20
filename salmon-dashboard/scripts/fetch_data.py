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


def share(part, whole):
    if part is None or whole in (None, 0):
        return None
    return round(part / whole * 100.0, 1)


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


def _norm_code(c: str) -> str:
    return re.sub(r"\D", "", str(c))


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
        vals, texts = vars_[var_code]["values"], vars_[var_code]["valueTexts"]
        salmon_like = [(v, t) for v, t in zip(vals, texts) if _norm_code(v)[:4] in ("0302", "0303") and ("salmon" in t.lower() or "laks" in t.lower())]
        log(f"  {table}: commodity values sample={vals[:3]} {texts[:3]}; salmon-like 0302/0303 codes: " + "; ".join(f"{v}={t[:60]}" for v, t in salmon_like[:30]))
        present, labels = [], {}
        for v, t in zip(vals, texts):
            n = _norm_code(v)
            for w in wanted:
                if n[:8] == w or n == w:
                    present.append(v)
                    labels[v] = f"{wanted[w]} [{v}]"
        if not present:
            # fall back to any 0302/0303 code whose text mentions salmon and farmed/Atlantic
            for v, t in salmon_like:
                if "atlantic" in t.lower() or "farm" in t.lower() or "oppdrett" in t.lower():
                    present.append(v)
                    labels[v] = f"{t} [{v}]"
        if not present:
            errors.append(f"{table}: none of {list(wanted)} present (see log for salmon-like codes)")
            continue
        selections = {var_code: present}
        for c, v in vars_.items():
            txt = v["text"].lower()
            if c.lower() == "impeks" or ("import" in txt and "export" in txt):
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
        qty_code = next((c for c, l in contents.items() if "quantity" in l.lower() or "weight" in l.lower() or "mengde" in l.lower() or "kg" in l.lower() or "tonn" in l.lower()), None)
        val_code = next((c for c, l in contents.items() if "value" in l.lower() or "verdi" in l.lower() or "nok" in l.lower()), None)
        log(f"  contents: {contents}; qty={qty_code} val={val_code}")
        qty_scale, qty_label = _scale_from_label(contents.get(qty_code, "")) if qty_code else (1.0, "")
        val_scale, val_label = _scale_from_label(contents.get(val_code, "")) if val_code else (1.0, "")
        agg: dict[str, dict[str, dict]] = {}
        for r in recs:
            v = r["value"]
            if v is None:
                continue
            month = r["Tid"]
            country = r[country_dim + "_label"]
            ccode = r[country_dim]
            kind = "fresh" if _norm_code(r[var_code]).startswith("0302") else "frozen"
            cell = agg.setdefault(month, {}).setdefault(country, {"code": ccode, "fresh_tonnes": 0.0, "frozen_tonnes": 0.0, "fresh_mnok": 0.0, "frozen_mnok": 0.0})
            if r["ContentsCode"] == qty_code:
                cell[f"{kind}_tonnes"] += v * qty_scale / 1000.0
            elif r["ContentsCode"] == val_code:
                cell[f"{kind}_mnok"] += v * val_scale / 1e6
        months = sorted(agg)
        total_words = ("all countries", "total", "world", "alle land", "i alt", "unspecified")

        def is_total(name: str, code: str) -> bool:
            n = name.lower()
            return any(w in n for w in total_words) or code in ("0", "00", "999", "9999", "AA", "ZZ")

        monthly_total = []
        for m in months:
            cells = [c for n, c in agg[m].items() if not is_total(n, c["code"])]
            ft = sum(c["fresh_tonnes"] for c in cells)
            fz = sum(c["frozen_tonnes"] for c in cells)
            fv = sum(c["fresh_mnok"] for c in cells)
            zv = sum(c["frozen_mnok"] for c in cells)
            monthly_total.append({"month": m, "fresh_tonnes": r1(ft), "frozen_tonnes": r1(fz), "fresh_mnok": r1(fv), "frozen_mnok": r1(zv),
                                  "total_tonnes": r1(ft + fz), "total_mnok": r1(fv + zv),
                                  "fresh_nok_kg": r1(fv * 1e6 / (ft * 1000), 2) if ft > 0 else None})
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
                             "nok_kg": r1(tot_v * 1e6 / (tot_t * 1000), 2) if tot_t > 0 else None,
                             "tonnes_yoy_pct": pct(tot_t, py_t), "mnok_yoy_pct": pct(tot_v, py_v)})
            rows.sort(key=lambda x: -(x["mnok"] or 0))
        return {"table": table, "title": meta.get("title"), "hs_codes": labels, "quantity_label": qty_label, "value_label": val_label,
                "latest_month": latest, "monthly_total": monthly_total, "by_country_latest": rows[:25], "country_count": len(rows)}
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
        pairs = list(zip(v["values"], v["valueTexts"]))
        sal = [val for val, t in pairs if t.lower().strip() in ("salmon", "laks") or t.lower().startswith("salmon")]
        if sal:
            selections[c] = sal[:1]
            continue
        # any other dimension (e.g. region): take the national total only
        tot = [val for val, t in pairs if any(w in t.lower() for w in ("whole country", "the whole", "norway", "total", "hele landet", "i alt"))]
        selections[c] = tot[:1] if tot else [v["values"][0]]
    log(f"  selections: {selections}")
    ds = ssb_query(table, meta, selections, top_time=15)
    recs = jsonstat_records(ds)
    out: dict[str, dict] = {}
    labels: dict[str, str] = {}
    for r in recs:
        row = out.setdefault(r["Tid"], {"year": r["Tid"]})
        label = str(r["ContentsCode_label"])
        l = label.lower()
        if "tonn" in l or "quantity" in l:
            key, scale = "tonnes", 1.0
        elif "nok" in l or "value" in l:
            factor, _ = _scale_from_label(label)
            key, scale = "mnok", factor / 1e6
        else:
            key, scale = re.sub(r"\W+", "_", l).strip("_"), 1.0
        labels[key] = label
        if r["value"] is not None:
            row[key] = round((row.get(key) or 0) + r["value"] * scale, 1)
    series = sorted(out.values(), key=lambda x: x["year"])
    for row in series:
        if row.get("tonnes") and row.get("mnok"):
            row["nok_kg"] = round(row["mnok"] * 1e6 / (row["tonnes"] * 1000), 2)
    return {"table": table, "title": meta.get("title"), "selection": selections, "labels": labels, "series": series}


# --------------------------------------------------------------------------- Fiskeridirektoratet biomass (Excel)
PARSER_VERSION = "1.0-total-omr"
MONTHS_NO = {1: "januar", 2: "februar", 3: "mars", 4: "april", 5: "mai", 6: "juni", 7: "juli", 8: "august", 9: "september", 10: "oktober", 11: "november", 12: "desember"}


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
        page_used = r.url
        for m in re.finditer(r'<a[^>]+href="([^"]+\.xlsx?)(?:\?[^"]*)?"[^>]*>(.*?)</a>', r.text, flags=re.I | re.S):
            href, text = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if href.startswith("/"):
                href = "https://www.fiskeridir.no" + href
            links.append({"href": href, "text": re.sub(r"\s+", " ", text), "file": href.rsplit("/", 1)[-1]})
        if links:
            break
    log(f"  biomass page: {page_used}; {len(links)} xlsx links: " + ", ".join(l["file"] for l in links))
    if not links:
        raise RuntimeError("no xlsx links found on biomass pages")
    total = next((l for l in links if "total" in l["file"].lower()), None)
    if not total:
        raise RuntimeError("biostat-total-omr.xlsx not found among links: " + ", ".join(l["file"] for l in links))
    content = http("GET", total["href"], headers={"Accept": "*/*"}, timeout=180).content
    if KEEP_RAW:
        (RAW / "biomass").mkdir(parents=True, exist_ok=True)
        (RAW / "biomass" / total["file"]).write_bytes(content)
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    ws = next((w for w in wb.worksheets if "prod" in w.title.lower() or "biomasse" in w.title.lower()), wb.worksheets[-1])
    rows = list(ws.iter_rows(values_only=True))
    as_of = None
    header_idx = None
    for i, row in enumerate(rows[:40]):
        c0 = str(row[0]).strip() if row and row[0] is not None else ""
        m = re.search(r"pr\.?\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", c0)
        if m and not as_of:
            as_of = f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"
        if c0.upper() == "ÅR":
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError("header row 'ÅR' not found in biomass total sheet")
    cols = [re.sub(r"\s+", "", str(c)).upper() if c is not None else "" for c in rows[header_idx]]
    idx = {c: i for i, c in enumerate(cols) if c}
    log(f"  biomass sheet '{ws.title}': {len(rows) - header_idx - 1} data rows; as_of={as_of}; columns={list(idx)}")
    need = ["ÅR", "MÅNED_KODE", "PO_KODE", "ARTSID"]
    for n in need:
        if n not in idx:
            raise RuntimeError(f"column {n} missing in biomass sheet; have {list(idx)}")
    measures = {
        "BEHFISK_STK": ("fish_mill", 1e-6), "BIOMASSE_KG": ("biomass_t", 1e-3), "UTSETT_SMOLT_STK": ("smolt_mill", 1e-6),
        "FORFORBRUK_KG": ("feed_t", 1e-3), "UTTAK_STK": ("harvest_mill", 1e-6), "UTTAK_KG": ("harvest_t", 1e-3),
        "DØDFISK_STK": ("dead_mill", 1e-6), "UTKAST_STK": ("discard_mill", 1e-6), "RØMMING_STK": ("escaped_count", 1.0),
        "ANDRE_STK": ("other_loss_mill", 1e-6), "TAP_ANNET_NY_STK": ("other_loss_new_mill", 1e-6), "TAP_TELLEFEIL_STK": ("count_adjust_mill", 1e-6),
    }
    present_measures = {k: v for k, v in measures.items() if k in idx}
    agg: dict[tuple, dict] = {}
    for row in rows[header_idx + 1:]:
        if not row or row[idx["ÅR"]] is None:
            continue
        try:
            year = int(row[idx["ÅR"]]); month = int(row[idx["MÅNED_KODE"]])
        except (TypeError, ValueError):
            continue
        po = row[idx["PO_KODE"]]
        po = str(int(po)) if isinstance(po, (int, float)) else (None if po in (None, "(null)", "") else str(po))
        species = str(row[idx["ARTSID"]] or "").upper()
        key = (year, month, po, species)
        d = agg.setdefault(key, {})
        for col, (name, scale) in present_measures.items():
            v = fnum(row[idx[col]])
            if v is not None:
                d[name] = d.get(name, 0.0) + v * scale

    def series_for(species_filter, po_filter):
        out: dict[str, dict] = {}
        for (y, m, po, sp), d in agg.items():
            if species_filter and sp != species_filter:
                continue
            if po_filter == "areas_only" and po is None:
                continue
            if po_filter not in (None, "areas_only") and po != po_filter:
                continue
            k = f"{y}-{m:02d}"
            o = out.setdefault(k, {"month": k, "year": y, "month_no": m})
            for name, v in d.items():
                o[name] = o.get(name, 0.0) + v
        res = sorted(out.values(), key=lambda x: x["month"])
        for o in res:
            for name in list(o):
                if isinstance(o[name], float):
                    o[name] = round(o[name], 3 if name.endswith("_mill") else 1)
            fish = o.get("fish_mill") or 0
            dead = o.get("dead_mill") or 0
            o["mortality_pct"] = round(dead / (fish + dead) * 100, 2) if fish + dead > 0 else None
        return res

    national = series_for("LAKS", None)
    trout = series_for("REGNBUEØRRET", None)
    by_area = {}
    for po in [str(i) for i in range(1, 14)]:
        ser = series_for("LAKS", po)
        if ser:
            by_area[po] = ser[-36:]
    latest = national[-1]["month"] if national else None
    # year-over-year on the national series
    nat_idx = {o["month"]: o for o in national}
    for o in national:
        prev = nat_idx.get(f"{o['year'] - 1}-{o['month_no']:02d}")
        o["biomass_t_yoy_pct"] = pct(o.get("biomass_t"), prev.get("biomass_t")) if prev else None
        o["harvest_t_yoy_pct"] = pct(o.get("harvest_t"), prev.get("harvest_t")) if prev else None
    year_totals: dict[int, dict] = {}
    for o in national:
        yt = year_totals.setdefault(o["year"], {"year": o["year"], "months": 0, "harvest_t": 0.0, "feed_t": 0.0, "smolt_mill": 0.0, "dead_mill": 0.0, "escaped_count": 0.0})
        yt["months"] += 1
        for k in ("harvest_t", "feed_t", "smolt_mill", "dead_mill", "escaped_count"):
            yt[k] = round(yt[k] + (o.get(k) or 0), 3)
    return {"page": page_used, "file": total, "as_of": as_of, "parser_version": PARSER_VERSION, "latest_month": latest,
            "measures": {v[0]: k for k, v in present_measures.items()},
            "units": {"biomass_t": "tonnes standing biomass at month end (LAKS)", "fish_mill": "million fish at month end", "harvest_t": "tonnes round weight (WFE) harvested in month",
                      "feed_t": "tonnes feed used in month", "smolt_mill": "million smolt released in month", "dead_mill": "million fish registered dead in month",
                      "escaped_count": "fish registered as escaped in month", "mortality_pct": "dead / (stock + dead) in month, %"},
            "national_latest": national[-1] if national else {},
            "national": national[-60:], "trout_national": trout[-24:], "by_area": by_area,
            "year_totals": sorted(year_totals.values(), key=lambda x: x["year"])[-6:],
            "all_links": [{"file": l["file"], "href": l["href"]} for l in links]}


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


def split_list(v) -> list[str]:
    if v is None:
        return []
    return [x.strip() for x in str(v).split(",") if x.strip()]


def fetch_fdir_register() -> dict:
    layer = CONFIG["fiskeridir"]["register_layer"]
    info = gis_layer_info(layer)
    rows = gis_query_all(layer, info)
    if rows:
        log("  sample row: " + json.dumps(rows[0], ensure_ascii=False, default=str)[:800])
    date_fields = {f["name"] for f in info.get("fields", []) if f["type"] == "esriFieldTypeDate"}
    company_patterns = [p.upper() for p in CONFIG["company"]["name_patterns"]]
    recs = []
    for r in rows:
        for k in date_fields:
            if k in r:
                r[k] = epoch_ms_to_date(r[k])
        holders = split_list(r.get("til_innehavere"))
        species = split_list(r.get("til_arter"))
        purposes = [p.upper() for p in split_list(r.get("til_formaal"))]
        forms = split_list(r.get("til_produksjonsform"))
        licences = split_list(r.get("til_tillatelser"))
        company_holders = [h for h in holders if any(p in h.upper() for p in company_patterns)]
        unit = str(r.get("kapasitet_unittype") or "")
        cap = fnum(r.get("kapasitet_lok"))
        pa = r.get("prodareacode")
        pa = str(int(pa)) if isinstance(pa, (int, float)) else (str(pa).strip() if pa not in (None, "") else None)
        salmonid = any(sp.lower() in ("laks", "regnbueørret", "ørret") for sp in species)
        rec = {
            "locality_no": r.get("loknr"), "locality_name": r.get("navn"), "status": r.get("status_lokalitet"),
            "cleared": r.get("klareringsdato"), "capacity_t": cap if unit.upper() == "TN" else None, "capacity_raw": cap, "capacity_unit": unit,
            "placement": r.get("plassering"), "water": r.get("vannmiljo"), "county": r.get("fylke"), "municipality": r.get("kommune"),
            "production_area": pa, "lat": r.get("lat"), "lon": r.get("lon"), "symbol": r.get("symbol"),
            "species": species, "holders": holders, "licences": licences, "purposes": purposes, "production_forms": forms,
            "url": r.get("lokalitet_url_ekstern") or r.get("lokalitet_url"),
            "is_company": bool(company_holders), "company_holders": company_holders, "company_sole": bool(company_holders) and len(holders) == len(company_holders),
            "is_salmonid": salmonid,
            "is_sea_foodfish": salmonid and str(r.get("plassering") or "").upper() == "SJØ" and any("matfisk" in f.lower() for f in forms) and ("KOMMERSIELL" in purposes),
        }
        recs.append(rec)
    sea = [r for r in recs if r["is_sea_foodfish"]]
    company_all = [r for r in recs if r["is_company"]]
    company_sea = [r for r in sea if r["is_company"]]
    entities: dict[str, int] = {}
    for r in company_all:
        for h in r["company_holders"]:
            entities[h] = entities.get(h, 0) + 1
    log(f"  register: {len(recs)} rows; sea food-fish salmonid={len(sea)}; company any={len(company_all)} sea={len(company_sea)}; entities={entities}")

    def by_area(sub):
        out = {}
        for r in sub:
            pa = r["production_area"] or "other"
            d = out.setdefault(pa, {"production_area": pa, "name": CONFIG["production_areas"].get(pa, "Broodstock / research / other"), "localities": 0, "capacity_t": 0.0, "active": 0})
            d["localities"] += 1
            d["capacity_t"] += r["capacity_t"] or 0
            d["active"] += 1 if str(r["status"] or "").upper() == "AKTIV" else 0
        for d in out.values():
            d["capacity_t"] = round(d["capacity_t"], 0)
        return sorted(out.values(), key=lambda d: (d["production_area"] == "other", len(d["production_area"]), d["production_area"]))

    def holder_counts(sub, top=15):
        cnt: dict[str, dict] = {}
        for r in sub:
            for h in r["holders"]:
                d = cnt.setdefault(h, {"holder": h, "localities": 0, "capacity_t": 0.0})
                d["localities"] += 1
                d["capacity_t"] += r["capacity_t"] or 0
        res = sorted(cnt.values(), key=lambda d: -d["localities"])[:top]
        for d in res:
            d["capacity_t"] = round(d["capacity_t"], 0)
            d["is_company"] = any(p in d["holder"].upper() for p in company_patterns)
        return res

    industry_area = by_area(sea)
    company_area = {d["production_area"]: d for d in by_area(company_sea)}
    areas = []
    for d in industry_area:
        c = company_area.get(d["production_area"], {})
        areas.append({**d, "company_localities": c.get("localities", 0), "company_capacity_t": c.get("capacity_t", 0.0),
                      "company_share_localities_pct": share(c.get("localities", 0), d["localities"]),
                      "company_share_capacity_pct": share(c.get("capacity_t", 0.0), d["capacity_t"])})
    company_localities = []
    for r in sorted(company_sea, key=lambda x: ((x["production_area"] or "99").zfill(2), str(x["locality_name"]))):
        company_localities.append({k: r[k] for k in ("locality_no", "locality_name", "status", "capacity_t", "capacity_unit", "county", "municipality", "production_area",
                                                     "lat", "lon", "species", "holders", "company_holders", "company_sole", "licences", "url")})
    compact = [{"n": r["locality_no"], "name": r["locality_name"], "pa": r["production_area"], "lat": r["lat"], "lon": r["lon"], "cap": r["capacity_t"],
                "mowi": r["is_company"], "sole": r["company_sole"], "status": r["status"], "holders": r["holders"][:4]} for r in sea]
    return {
        "layer": layer, "fields": [f["name"] for f in info.get("fields", [])], "row_count": len(recs),
        "industry_sea_foodfish": {"localities": len(sea), "active": sum(1 for r in sea if str(r["status"] or "").upper() == "AKTIV"),
                                  "capacity_t": round(sum(r["capacity_t"] or 0 for r in sea), 0), "by_area": areas, "top_holders": holder_counts(sea)},
        "company": {"name": CONFIG["company"]["name"], "entities": [{"holder": h, "localities": n} for h, n in sorted(entities.items(), key=lambda kv: -kv[1])],
                    "localities": len(company_sea), "localities_sole": sum(1 for r in company_sea if r["company_sole"]),
                    "localities_any_type": len(company_all), "active": sum(1 for r in company_sea if str(r["status"] or "").upper() == "AKTIV"),
                    "capacity_t": round(sum(r["capacity_t"] or 0 for r in company_sea), 0),
                    "licences": len({l for r in company_sea for l in r["licences"]}),
                    "by_area": [a for a in areas if a["company_localities"]],
                    "localities_list": company_localities},
        "_localities": compact,
    }


def fetch_fdir_escapes() -> dict:
    root = CONFIG["fiskeridir"]["gis_root"]
    folder = gis_json(root + "/Yggdrasil")
    kws = [k.lower() for k in CONFIG["fiskeridir"]["escapes_keywords"]]
    cands = [s for s in folder.get("services", []) if any(k in s["name"].lower() for k in kws)]
    if not cands:
        raise RuntimeError("no escape (rømming) service found in Yggdrasil folder: " + ", ".join(s["name"] for s in folder.get("services", [])))
    cands.sort(key=lambda s: 0 if s["type"] == "FeatureServer" else 1)
    svc = cands[0]
    svc_url = f"{root}/{svc['name']}/{svc['type']}"
    svc_info = gis_json(svc_url)
    layers_used = []
    all_rows: list[dict] = []
    for lyr in svc_info.get("layers", []):
        layer_url = f"{svc_url}/{lyr['id']}"
        info = gis_layer_info(layer_url)
        if info.get("type") not in ("Feature Layer", "Table"):
            continue
        rows = gis_query_all(layer_url, info, geometry=True)
        date_fields = {f["name"] for f in info.get("fields", []) if f["type"] == "esriFieldTypeDate"}
        for r in rows:
            for k in date_fields:
                if k in r:
                    r[k] = epoch_ms_to_date(r[k])
        all_rows.extend(rows)
        layers_used.append({"url": layer_url, "name": lyr.get("name"), "rows": len(rows)})
        break  # one layer holds the incidents
    if all_rows:
        log("  sample: " + json.dumps(all_rows[-1], ensure_ascii=False, default=str)[:900])
    seen = set()
    incidents = []
    for r in all_rows:
        key = r.get("globalid") or (r.get("objectid"), r.get("rommingsdato"), r.get("loknr"))
        if key in seen:
            continue
        seen.add(key)
        est_txt = r.get("antall_romt_estimert")
        est_num = fnum(est_txt)
        if est_num is None and est_txt:
            m = re.findall(r"\d[\d\s]*", str(est_txt).replace(" ", ""))
            nums = [fnum(x) for x in m if fnum(x) is not None]
            est_num = max(nums) if nums else None
        rec = {
            "id": r.get("globalid") or r.get("objectid"), "date": r.get("rommingsdato") or r.get("rommingsdato_antatt"), "date_assumed": r.get("rommingsdato_antatt"),
            "locality_no": r.get("loknr"), "locality_name": r.get("navn"), "company": r.get("selskapsnavn"), "species": r.get("art"),
            "description": r.get("beskrivelse"), "report_stage": r.get("status"), "estimated_range": est_txt, "estimated_max": est_num,
            "escaped_fish": fnum(r.get("antall_romt_fisk")), "size_g": fnum(r.get("storrelse_estimert")) or fnum(r.get("storrelse")),
            "recapture_started": r.get("gjenfangst_iverksatt"), "recaptured": fnum(r.get("gjenfangst_gjennomfort")), "recapture_note": r.get("gjenfangst_beskrivelse"),
            "cleanerfish": r.get("rensefisk"), "county": r.get("fylke"), "municipality": r.get("kommune"), "lat": r.get("lat"), "lon": r.get("lon"),
            "locality_capacity_t": fnum(r.get("kapsitet_lok")),
        }
        rec["is_company"] = matches_company(rec["company"])
        incidents.append(rec)
    incidents.sort(key=lambda x: str(x["date"] or ""), reverse=True)
    cutoff = (NOW - dt.timedelta(days=730)).date().isoformat()
    recent = [i for i in incidents if str(i["date"] or "") >= cutoff]

    def year_stats(year: str):
        rows = [r for r in incidents if str(r["date"] or "").startswith(year)]
        salmon = [r for r in rows if "laks" in str(r["species"] or "").lower()]
        return {"reports": len(rows), "salmon_reports": len(salmon),
                "salmon_fish_confirmed": round(sum(r["escaped_fish"] or 0 for r in salmon)),
                "salmon_fish_estimated_max": round(sum((r["escaped_fish"] if r["escaped_fish"] is not None else (r["estimated_max"] or 0)) for r in salmon)),
                "company_reports": sum(1 for r in rows if r["is_company"]),
                "company_fish_confirmed": round(sum(r["escaped_fish"] or 0 for r in rows if r["is_company"]))}

    years = sorted({str(i["date"])[:4] for i in incidents if i["date"]})
    return {"service": svc_url, "layers": layers_used, "total_reports": len(incidents), "first_year": years[0] if years else None,
            "recent": recent[:400], "company_recent": [i for i in recent if i["is_company"]],
            "by_year": {y: year_stats(y) for y in years[-6:]}}


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
        for r in register.get("_localities") or []:
            if r.get("n") is not None and r.get("pa"):
                loc_to_area[str(r["n"])] = str(r["pa"])
        for r in register.get("company", {}).get("localities_list", []):
            company_locs.add(str(r["locality_no"]))
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
        add("biomass", f"New biomass month {bm['latest_month']}: {nat.get('biomass_t', 0):,.0f} t salmon standing biomass" + (f" ({nat['biomass_t_yoy_pct']:+.1f}% y/y)" if nat.get("biomass_t_yoy_pct") is not None else ""),
            f"Harvest {nat.get('harvest_t', 0):,.0f} t WFE" + (f" ({nat['harvest_t_yoy_pct']:+.1f}% y/y)" if nat.get("harvest_t_yoy_pct") is not None else "") +
            f"; {nat.get('dead_mill', 0):.1f} million fish dead ({nat.get('mortality_pct')}% of stock); feed {nat.get('feed_t', 0):,.0f} t; smolt released {nat.get('smolt_mill', 0):.1f} million.",
            "notable")

    # register / company
    reg = latest.get("register") or {}
    preg = (previous or {}).get("register") or {}
    if reg.get("company"):
        c, pc = reg["company"], preg.get("company") or {}
        if not previous:
            add("company", f"{c['name']}: {c['localities']} sea food-fish localities, {c['licences']} licences in the register",
                f"Locality capacity {c['capacity_t']:,.0f} t; entities: " + ", ".join(e['holder'] for e in c['entities']), "info", company=True)
        else:
            cur_l = {str(x["locality_no"]): x for x in c.get("localities_list", [])}
            old_l = {str(x["locality_no"]): x for x in pc.get("localities_list", [])}
            for k in sorted(set(cur_l) - set(old_l)):
                x = cur_l[k]
                add("company", f"New {c['name']} locality in register: {x['locality_name']} ({k})", f"PO{x['production_area']} {x.get('municipality') or ''}; capacity {x.get('capacity_t') or 0:,.0f} t", "notable", company=True)
            for k in sorted(set(old_l) - set(cur_l)):
                x = old_l[k]
                add("company", f"{c['name']} locality removed from register: {x['locality_name']} ({k})", f"PO{x['production_area']}", "notable", company=True)
            if c.get("licences") != pc.get("licences"):
                add("company", f"{c['name']} licence count {pc.get('licences')} → {c.get('licences')}", "", "notable", company=True)
            if c.get("capacity_t") != pc.get("capacity_t") and pc.get("capacity_t"):
                add("company", f"{c['name']} locality capacity {pc['capacity_t']:,.0f} → {c['capacity_t']:,.0f} t", "", "info", company=True,
                    delta_pct=pct(c["capacity_t"], pc["capacity_t"]))
        ind, pind = reg.get("industry_sea_foodfish") or {}, preg.get("industry_sea_foodfish") or {}
        if previous and ind and pind and ind.get("localities") != pind.get("localities"):
            add("industry", f"Industry sea localities in register {pind['localities']} → {ind['localities']}", "", "info")

    # escapes
    esc = latest.get("escapes") or {}
    pesc = (previous or {}).get("escapes") or {}
    if esc.get("recent"):
        old_ids = {(str(i.get("id")), str(i.get("date"))) for i in pesc.get("recent", [])}
        new = [i for i in esc["recent"] if (str(i.get("id")), str(i.get("date"))) not in old_ids] if previous else esc["recent"][:5]
        for i in new[:12]:
            n = i.get("escaped_fish")
            qty = f"{n:,.0f} fish confirmed" if n is not None else (f"estimated {i.get('estimated_range')} fish" if i.get("estimated_range") else "number not yet reported")
            add("escapes", f"{'New ' if previous else 'Recent '}escape report {i.get('date')}: {i.get('locality_name') or i.get('locality_no')} ({i.get('company') or 'company n/a'})",
                f"{i.get('species') or ''}; {qty}; {i.get('report_stage') or ''}. {i.get('description') or ''}".strip(),
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
    if latest.get("register") and latest["register"].get("_localities"):
        save_json(DATA / "register_localities.json", {"generated_at": latest["generated_at"], "localities": latest["register"].pop("_localities")})
    elif latest.get("register"):
        latest["register"].pop("_localities", None)
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
