"""Construye el tablero HTML autocontenido a partir del warehouse SQLite.

Salidas:
  out/dashboard.html          tablero operativo (se abre con doble clic)
  out/pronostico_resumen.csv  tabla plana para reutilizar en Excel/BI
  out/dashboard_data.json     mismos datos en JSON

El indice de riesgo es una heuristica transparente v0. NO es un modelo
entrenado; su proposito es ordenar la vigilancia y servir de linea base
contra la que se medira el modelo ML mas adelante.
"""

from __future__ import annotations

import csv
import html
import json
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

from common import CACHE_DIR, DATA_DIR, OUT_DIR, banner, db, get_meta
from build_prices import ALIASES, norm

GEODATA_PATH = DATA_DIR / "geodata" / "waterways.json"

# --------------------------------------------------------------------------- #
# Parametros del indice de riesgo (documentados en el tablero y el README)
# --------------------------------------------------------------------------- #
FLOOR_MM_24H = 10.0     # piso absoluto: por debajo de 10 mm/dia no hay riesgo real en la costa
FLOOR_MM_72H = 20.0     # piso para el acumulado de 3 dias
INTENSITY_REF = 20.0    # mm/h que satura la componente de intensidad
W_VOLUME, W_INTENSITY, W_PROB = 0.40, 0.35, 0.25

LEVEL_STYLE = {
    "Bajo":     {"bar": "#2e9e63", "fg": "#166534", "bg": "#e7f6ee"},
    "Medio":    {"bar": "#d9a520", "fg": "#8a6100", "bg": "#fdf3d8"},
    "Alto":     {"bar": "#e2731c", "fg": "#a3400a", "bg": "#fde8dc"},
    "Muy alto": {"bar": "#c0392b", "fg": "#a51c1c", "bg": "#fbe3e3"},
}


def level_for(score: float) -> str:
    if score >= 0.55:
        return "Muy alto"
    if score >= 0.38:
        return "Alto"
    if score >= 0.20:
        return "Medio"
    return "Bajo"


def heat_color(value: float | None) -> tuple[str, str]:
    """Devuelve (fondo, color de texto) para una celda del mapa de calor."""
    if value is None or value < 0.1:
        return "#fbfcfd", "#c3ccd6"
    if value < 2:
        return "#dcefF7", "#22303f"
    if value < 5:
        return "#a9d6e9", "#15303f"
    if value < 10:
        return "#6fb8d8", "#0e2633"
    if value < 25:
        return "#f0b03f", "#3a2600"
    if value < 50:
        return "#e2694a", "#ffffff"
    return "#a81f1f", "#ffffff"


def fmt(value, decimals: int = 1, dash: str = "—") -> str:
    if value is None:
        return dash
    try:
        v = float(value)
    except (TypeError, ValueError):
        return dash
    if v != v:  # NaN
        return dash
    return f"{v:,.{decimals}f}".replace(",", " ")


# --------------------------------------------------------------------------- #
# Carga de datos
# --------------------------------------------------------------------------- #
def load_state(conn, run_date: str):
    points = [dict(r) for r in conn.execute("SELECT * FROM points ORDER BY lat DESC")]
    thr = {r["point_id"]: dict(r) for r in conn.execute("SELECT * FROM thresholds")}

    hourly: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for r in conn.execute(
        "SELECT point_id, model, ts, precip_mm FROM forecast_hourly "
        "WHERE run_date = ? ORDER BY ts",
        (run_date,),
    ):
        hourly[(r["point_id"], r["model"])].append((r["ts"], r["precip_mm"]))

    ens: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute(
        "SELECT * FROM ensemble_daily WHERE run_date = ? ORDER BY date", (run_date,)
    ):
        ens[r["point_id"]].append(dict(r))

    return points, thr, hourly, ens


def summarise_point(point, thr_row, hourly, ens_rows, today: date) -> dict:
    pid = point["id"]
    models = sorted({m for (p, m) in hourly if p == pid})
    best = "ecmwf_ifs025" if (pid, "ecmwf_ifs025") in hourly else (models[0] if models else None)
    series = hourly.get((pid, best), []) if best else []

    next24 = sum(v for _, v in series[:24])
    next72 = sum(v for _, v in series[:72])
    max_hourly = max((v for _, v in series[:72]), default=0.0)

    daily: dict[str, float] = defaultdict(float)
    for ts, v in series:
        daily[ts[:10]] += v

    spread = []
    for model in models:
        s = hourly.get((pid, model), [])
        if len(s) >= 72:
            spread.append(sum(v for _, v in s[:72]))
    spread_std = float(np.std(spread)) if len(spread) > 1 else 0.0

    # Excedencia del ensemble en la ventana de 4 dias.
    upcoming = [r for r in ens_rows if r["date"] >= today.isoformat()][:4]
    prob25, prob25_date, prob10 = 0.0, None, 0.0
    for r in upcoming:
        p25 = r.get("prob25") or 0.0
        if p25 > prob25:
            prob25, prob25_date = p25, r["date"]
        prob10 = max(prob10, r.get("prob10") or 0.0)

    p95 = (thr_row or {}).get("p95_wet")
    p95_3d = (thr_row or {}).get("p95_3d_wet")

    # Dos escalas de comparacion: el dia extremo y el acumulado que satura el suelo.
    d24 = max(p95 if (p95 and p95 > 0) else 0.0, FLOOR_MM_24H)
    d72 = max(p95_3d if (p95_3d and p95_3d > 0) else 0.0, FLOOR_MM_72H)
    ratio_24 = next24 / d24
    ratio_72 = next72 / d72
    ratio = max(ratio_24, ratio_72)

    # Normalizacion: estar en el p95 climatologico equivale a 0,5 (ya es notable);
    # duplicar ese umbral satura la componente en 1,0.
    volume = min(ratio / 2.0, 1.0)
    intensity = min(max_hourly / INTENSITY_REF, 1.0)
    score = W_VOLUME * volume + W_INTENSITY * intensity + W_PROB * prob25

    return {
        "id": pid,
        "name": point["name"],
        "dept": point["dept"],
        "rol": point["rol"],
        "lat": point["lat"],
        "lon": point["lon"],
        "model": best,
        "next24": next24,
        "next72": next72,
        "max_hourly": max_hourly,
        "spread_std": spread_std,
        "p95": p95,
        "p95_3d": p95_3d,
        "ratio": ratio,
        "p99": (thr_row or {}).get("p99_wet"),
        "max_daily": (thr_row or {}).get("max_daily"),
        "wet_days": (thr_row or {}).get("wet_days_per_year"),
        "prob25": prob25,
        "prob25_date": prob25_date,
        "prob10": prob10,
        "score": round(score, 3),
        "level": level_for(score),
        "daily": dict(sorted(daily.items())),
    }


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
CSS = """
:root{
  --bg:#f5f1e8; --card:#fffdf8; --text:#211d18; --muted:#746a5d;
  --line:#e7ddca; --accent:#5a3b22; --accent2:#d39b00; --edo-yellow:#f4c300;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-size:14px;line-height:1.5}
.wrap{max-width:1180px;margin:0 auto;padding:22px 20px 60px}
.nav{display:flex;gap:8px;margin-bottom:12px}
.nav a{padding:8px 14px;border:1px solid #d4b75b;border-radius:999px;text-decoration:none;
  color:#4b351d;background:#fffdf8;font-weight:650}
.nav a.active{background:var(--edo-yellow);border-color:var(--edo-yellow)}
header.hero{
  background:linear-gradient(135deg,#15130f 0%,#2a2118 58%,#654621 100%);
  color:#fff;border-radius:14px;padding:26px 28px;margin-bottom:18px;
  border-top:5px solid var(--edo-yellow);
  box-shadow:0 6px 22px rgba(11,58,84,.22)}
header.hero h1{margin:0 0 6px;font-size:23px;letter-spacing:-.2px}
header.hero p{margin:0;color:#f4e9ca;font-size:13.5px;max-width:760px}
.hero-meta{margin-top:14px;display:flex;flex-wrap:wrap;gap:8px}
.pill{background:rgba(244,195,0,.14);border:1px solid rgba(244,195,0,.42);
  padding:4px 10px;border-radius:999px;font-size:12px;color:#ffe889}
h2{font-size:15px;margin:0 0 12px;color:#3b2a1c;letter-spacing:.2px;
  text-transform:uppercase;font-weight:700}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:18px 20px;margin-bottom:16px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi .label{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);font-weight:600}
.kpi .value{font-size:26px;font-weight:700;margin-top:4px;letter-spacing:-.5px}
.kpi .sub{font-size:12px;color:var(--muted);margin-top:2px}
.transect{display:flex;flex-direction:column;gap:3px}
.trow{display:grid;grid-template-columns:186px 1fr 92px;gap:10px;align-items:center;
  padding:3px 0;border-radius:6px}
.trow:hover{background:#fbf6e9}
.tname{font-size:12.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tname span{font-weight:400;color:var(--muted);font-size:11.5px}
.tbar{background:#f0f3f7;border-radius:5px;height:17px;position:relative;overflow:hidden}
.tfill{height:100%;border-radius:5px;min-width:2px;transition:width .3s}
.tval{text-align:right;font-variant-numeric:tabular-nums;font-size:12.5px;font-weight:600}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-top:12px;font-size:11.5px;color:var(--muted)}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:5px;vertical-align:-1px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);
  font-weight:700;background:#faf5e8;position:sticky;top:0;z-index:2}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tbody tr:hover{background:#fbf6e9}
.chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;
  font-weight:700;letter-spacing:.2px}
.heat td.cell{text-align:center;font-size:10.5px;font-variant-numeric:tabular-nums;
  border:1px solid #fff;border-radius:3px;padding:4px 2px;min-width:38px}
.heat th.rot{font-size:10px;text-align:center;padding:4px 2px}
.heat .sticky-col{position:sticky;left:0;background:#fff;z-index:1}
.scroll{overflow-x:auto;margin:0 -4px}
.note{font-size:12.5px;color:#51483e;background:#faf5e8;border-left:3px solid var(--edo-yellow);
  padding:10px 14px;border-radius:0 8px 8px 0;margin-top:12px}
details{margin-top:7px;border-top:1px solid var(--line);padding-top:9px}
summary{cursor:pointer;font-weight:600;font-size:12.5px;color:var(--accent);outline:none}
details p,details li{font-size:12.5px;color:#475569}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
code{background:#eef2f6;padding:1px 5px;border-radius:4px;font-size:11.5px}
footer{margin-top:24px;font-size:11.5px;color:var(--muted);text-align:center;line-height:1.7}
.maps{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}
.zmap{border:1px solid var(--line);border-radius:10px;padding:12px 12px 10px;background:#fdfefe}
.zmap h3{font-size:13px;margin:0 0 3px;color:#3b2a1c}
.zmap .sub{font-size:11px;color:var(--muted);margin-bottom:9px;
  display:flex;align-items:center;gap:7px}
.zmap svg{width:100%;height:auto;display:block;border-radius:7px}
.cauces{display:flex;flex-wrap:wrap;gap:5px 11px;margin-top:10px;font-size:11px;color:#475569}
.cauces span{display:inline-flex;align-items:center;gap:4px;white-space:nowrap}
.cauces i{width:8px;height:8px;border-radius:2px;display:inline-block;flex:0 0 auto}
.znota{margin-top:9px;font-size:10.5px;color:#64748b;line-height:1.45;
  border-top:1px dashed var(--line);padding-top:7px}
details.sec{background:var(--card);border:1px solid var(--line);border-radius:12px;
  margin-bottom:12px;box-shadow:0 1px 2px rgba(16,24,40,.04);overflow:hidden}
details.sec>summary{padding:14px 20px;cursor:pointer;list-style:none;
  display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
details.sec>summary::-webkit-details-marker{display:none}
details.sec>summary::before{content:"▸";color:var(--accent);font-size:13px;
  transition:transform .15s;display:inline-block}
details.sec[open]>summary::before{transform:rotate(90deg)}
details.sec>summary:hover{background:#fbf6e9}
.st{font-size:14px;font-weight:700;color:#3b2a1c;text-transform:uppercase;letter-spacing:.3px}
.sh{font-size:12px;color:var(--muted)}
.sbody{padding:0 20px 18px}
.nota-sec{font-size:12px;color:#64748b;margin:0 0 12px}
.subtitle{font-size:12px;color:#64748b;margin:-6px 0 12px}
.natwrap{display:grid;grid-template-columns:minmax(320px,600px) 1fr;gap:26px;align-items:start}
.natmap>svg{width:100%;height:auto;display:block;border-radius:10px}
.natside h4{margin:2px 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.5px;
  color:var(--muted)}
.alist{list-style:none;margin:0;padding:0}
.alist li{display:flex;align-items:center;gap:9px;padding:8px 0;
  border-bottom:1px solid var(--line);font-size:12.5px}
.alist li:last-child{border-bottom:none}
.alist i{width:10px;height:10px;border-radius:50%;flex:0 0 auto}
.alist .nm{font-weight:600}
.alist .mt{color:var(--muted);font-size:11px;margin-top:1px}
.alist .sp{margin-left:auto}
.natnota{margin-top:10px;font-size:11px;color:#64748b;line-height:1.5}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px}
.pill-dark{background:#f5eedf;border:1px solid var(--line);color:#5a4937;
  padding:3px 10px;border-radius:999px;font-size:11px;font-weight:600}
.dot{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:7px;
  vertical-align:-1px}
.via{font-weight:600}
.km{color:var(--muted);font-size:11px}
.nino-line{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 10px;margin-top:12px;
  padding-top:11px;border-top:1px dashed var(--line);font-size:12.5px;color:#3f5468}
.nk{font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;font-weight:700;
  color:#5a3b22;background:#f7e9b6;padding:2px 8px;border-radius:999px}
.nt strong{color:#3b2a1c}
.nesc{color:var(--muted);font-size:11.5px}
.aleg{display:flex;flex-direction:column;gap:5px;font-size:11.5px;color:#475569}
.aleg b{color:#1f3347;margin-left:2px}
.dm{display:inline-block;width:7px;height:7px;transform:rotate(45deg);
  margin-right:7px;border-radius:1px;vertical-align:1px}
.layers{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 14px}
.layers-lbl{font-size:10.5px;text-transform:uppercase;letter-spacing:.6px;
  font-weight:700;color:var(--muted);margin-right:2px}
.zoomctl{margin-left:auto;display:flex;align-items:center;gap:4px}
.zbtn{border:1px solid var(--line);background:#fffdf8;color:#4c3a28;width:28px;height:28px;
  border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;
  display:inline-flex;align-items:center;justify-content:center;line-height:1}
.zbtn:hover{background:#fff3bd;border-color:#d5ad28}
.zbtn[data-zoom="reset"]{width:auto;padding:0 10px;font-size:11px;font-weight:600}
.zlevel{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;
  min-width:40px;text-align:right}
.natmap>svg{cursor:grab;touch-action:none}
.natmap>svg:active{cursor:grabbing}
.tgl{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);
  background:#fff;color:#43596e;padding:6px 13px;border-radius:999px;font-size:12px;
  font-weight:600;cursor:pointer;font-family:inherit;line-height:1.2;
  transition:background .15s,border-color .15s,opacity .15s}
.tgl:hover{border-color:#a9c3d8;background:#f7fbfd}
.tgl:focus-visible{outline:2px solid #b88400;outline-offset:2px}
.tgl .sw{width:9px;height:9px;flex:0 0 auto}
.tgl .sw-cir{border-radius:50%;background:#d9a520}
.tgl .sw-dia{transform:rotate(45deg);border-radius:1px;background:#c0392b}
.tgl b{font-weight:700;color:#3b2a1c;font-variant-numeric:tabular-nums}
.tgl.on{background:#fff1af;border-color:#d5ad28;color:#4f3816}
.tgl.off{opacity:.45;background:#fff}
.mlegend{margin-top:14px;padding-top:13px;border-top:1px solid var(--line)}
.ml-title{font-size:10.5px;text-transform:uppercase;letter-spacing:.6px;font-weight:700;
  color:var(--muted);margin-bottom:10px}
.ml-groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px 18px}
.ml-gtitle{font-size:11px;font-weight:700;color:#1f3347;margin-bottom:6px}
.ml-items{display:flex;flex-wrap:wrap;gap:4px 12px;margin-bottom:5px}
.ml-levels{display:grid;grid-template-columns:auto auto;gap:3px 14px;justify-content:start}
.ml-i{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;color:#475569}
.ml-i svg{flex:0 0 auto;width:15px;height:15px;display:block}
.ml-note{font-size:10.5px;color:#7c8b9c;line-height:1.35}
.vcell{display:flex;align-items:center;gap:7px}
.vbar{width:62px;height:6px;background:#eef2f6;border-radius:3px;display:inline-block;
  overflow:hidden;flex:0 0 auto}
.vbar i{display:block;height:100%;border-radius:3px}
.vnum{font-variant-numeric:tabular-nums;font-size:11.5px;color:#475569}
.vtag{display:inline-block;padding:2px 8px;border-radius:999px;font-size:10.5px;font-weight:700}
.vtag.mal{background:#fbe3e3;color:#a51c1c}
.vtag.ok{background:#e7f6ee;color:#166534}
@media (max-width:820px){.grid2{grid-template-columns:1fr}
 .natwrap{grid-template-columns:1fr}
 .trow{grid-template-columns:132px 1fr 74px}}
"""


def chip(level: str) -> str:
    style = LEVEL_STYLE[level]
    return (
        f'<span class="chip" style="color:{style["fg"]};'
        f'background:{style["bg"]}">{level}</span>'
    )


def level_legend() -> str:
    items = "".join(
        f'<span><i style="background:{s["bar"]}"></i>{name}</span>'
        for name, s in LEVEL_STYLE.items()
    )
    return f'<div class="legend">{items}</div>'


def render_transect(rows: list[dict], title: str, subtitle: str, peak: float) -> str:
    if not rows:
        return ""
    rows = sorted(rows, key=lambda r: -r["lat"])  # norte -> sur, como anuncia el titulo
    peak = peak or 1.0
    bars = []
    for r in rows:
        style = LEVEL_STYLE[r["level"]]
        width = 0.0 if r["next72"] < 0.05 else max(r["next72"] / peak * 100.0, 1.0)
        bars.append(
            f'<div class="trow" title="{html.escape(r["name"])}: {fmt(r["next72"])} mm en 72 h · '
            f'p95 3d {fmt(r["p95_3d"])} mm · riesgo {r["level"]}">'
            f'<div class="tname">{html.escape(r["name"])} '
            f'<span>{html.escape(r["dept"])}</span></div>'
            f'<div class="tbar"><div class="tfill" '
            f'style="width:{width:.1f}%;background:{style["bar"]}"></div></div>'
            f'<div class="tval" style="color:{style["fg"]}">{fmt(r["next72"])} mm</div></div>'
        )
    return f'<div class="transect">{"".join(bars)}</div>{level_legend()}'


def render_heatmap(rows: list[dict], dates: list[str]) -> str:
    head = ['<th class="sticky-col" style="text-align:left">Punto</th>']
    for d in dates:
        head.append(f'<th class="rot">{d[8:10]}/{d[5:7]}</th>')
    body = []
    for r in rows:
        cells = [f'<td class="sticky-col" style="font-weight:600">{html.escape(r["name"])}</td>']
        for d in dates:
            v = r["daily"].get(d, 0.0)
            bg, fg = heat_color(v)
            txt = f"{v:.0f}" if v >= 0.5 else ""
            cells.append(
                f'<td class="cell" style="background:{bg};color:{fg}" '
                f'title="{html.escape(r["name"])} {d}: {v:.1f} mm">{txt}</td>'
            )
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (
        '<div class="scroll"><table class="heat"><thead><tr>'
        + "".join(head)
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
        + '<div class="legend">'
        '<span><i style="background:#fbfcfd;border:1px solid #dde4ec"></i>0</span>'
        '<span><i style="background:#a9d6e9"></i>&lt;5</span>'
        '<span><i style="background:#6fb8d8"></i>5–10</span>'
        '<span><i style="background:#f0b03f"></i>10–25</span>'
        '<span><i style="background:#e2694a"></i>25–50</span>'
        '<span><i style="background:#a81f1f"></i>&gt;50 mm</span>'
        "</div>"
    )


def render_table(rows: list[dict]) -> str:
    body = []
    for r in rows:
        ratio = r["ratio"]
        body.append(
            "<tr>"
            f'<td style="font-weight:600">{html.escape(r["name"])}</td>'
            f'<td style="color:#64748b">{html.escape(r["dept"])}</td>'
            f'<td class="num">{fmt(r["next24"])}</td>'
            f'<td class="num">{fmt(r["next72"])}</td>'
            f'<td class="num">{fmt(r["max_hourly"], 1)}</td>'
            f'<td class="num">{fmt(r["p95"])}</td>'
            f'<td class="num">{fmt(r["p95_3d"])}</td>'
            f'<td class="num">{fmt(ratio, 1) + "×" if ratio else "—"}</td>'
            f'<td class="num">{fmt(r["prob25"] * 100, 0) + " %" if r["prob25"] else "—"}</td>'
            f'<td class="num">{fmt(r["spread_std"])}</td>'
            f"<td>{chip(r['level'])}</td>"
            "</tr>"
        )
    return (
        '<div class="scroll"><table><thead><tr>'
        '<th>Punto</th><th>Depto.</th>'
        '<th class="num">24 h</th><th class="num">72 h</th><th class="num">máx mm/h</th>'
        '<th class="num">p95 1d</th><th class="num">p95 3d</th><th class="num">vs umbral</th>'
        '<th class="num">P(≥25 mm)</th><th class="num">dispersión</th><th>Nivel</th>'
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
    )


# --------------------------------------------------------------------------- #
# Mapa nacional y mapas de zona
# --------------------------------------------------------------------------- #
PERU_PATH = DATA_DIR / "geodata" / "peru.json"
ROADS_PATH = DATA_DIR / "geodata" / "roads.json"
NAT_W, NAT_H, NAT_PAD = 620, 920, 22
PRIORITY_ZONES = {"chosica", "piura", "tumbes"}
ALERT_COLOUR = {
    "interrumpido": "#c0392b",
    "restringido": "#e2731c",
    "normal": "#2e9e63",
}
ALERT_LABEL = {
    "interrumpido": "Interrumpido",
    "restringido": "Restringido",
    "normal": "Normal",
}
MAP_W, MAP_H, MAP_PAD = 380, 300, 16

_KIND_ORDER = {"river": 0, "stream": 1}
_KIND_WIDTH = {"river": 3.0, "stream": 1.8}


def load_geodata() -> dict:
    if not GEODATA_PATH.exists():
        return {}
    try:
        return json.loads(GEODATA_PATH.read_text(encoding="utf-8")).get("zones", {})
    except (json.JSONDecodeError, OSError):
        return {}


def load_peru() -> dict:
    if not PERU_PATH.exists():
        return {}
    try:
        return json.loads(PERU_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_roads() -> dict:
    if not ROADS_PATH.exists():
        return {}
    try:
        return json.loads(ROADS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_verification(conn) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM rule_verification ORDER BY umbral")]
    except Exception:  # noqa: BLE001
        return []


def load_alerts(conn) -> list[dict]:
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM road_alerts")]
    except Exception:  # noqa: BLE001
        return []
    # Primero por estado de la vía (lo que corta el tránsito), y dentro de cada
    # estado por motivo: fenómenos naturales antes que accidentes.
    orden_estado = {"interrumpido": 0, "restringido": 1, "normal": 2}
    orden_motivo = {"fenomenos_naturales": 0, "infraestructura": 1, "accidentes": 2}
    rows.sort(
        key=lambda a: (
            orden_estado.get(a.get("estado_via"), 9),
            orden_motivo.get(a.get("motivo"), 9),
            a.get("via_nombre") or "",
        )
    )
    return rows


def load_nino(conn) -> dict:
    try:
        row = conn.execute("SELECT payload FROM nino_status WHERE id = 1").fetchone()
    except Exception:  # noqa: BLE001
        return {}
    if not row:
        return {}
    try:
        return json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return {}


def render_national_map(
    rows: list[dict], peru: dict, roads: dict | None = None, alerts: list[dict] | None = None
) -> str:
    """Mapa del Peru entero con la red vial y los puntos de alerta."""
    if not peru or not rows:
        return ""

    lons, lats = [], []
    for ring in peru.get("adm0", []):
        for lon, lat in ring:
            lons.append(lon)
            lats.append(lat)
    if not lons:
        return ""
    lon0, lon1, lat0, lat1 = min(lons), max(lons), min(lats), max(lats)

    kx = math.cos(math.radians((lat0 + lat1) / 2))
    span_x = max((lon1 - lon0) * kx, 1e-6)
    span_y = max(lat1 - lat0, 1e-6)
    scale = min((NAT_W - 2 * NAT_PAD) / span_x, (NAT_H - 2 * NAT_PAD) / span_y)
    off_x = (NAT_W - span_x * scale) / 2
    off_y = (NAT_H - span_y * scale) / 2

    def project(lon: float, lat: float) -> tuple[float, float]:
        return (off_x + (lon - lon0) * kx * scale, off_y + (lat1 - lat) * scale)

    def poly(ring: list, close: bool = True) -> str:
        pts = " L ".join(f"{x:.1f},{y:.1f}" for x, y in (project(c[0], c[1]) for c in ring))
        return f"M {pts}" + (" Z" if close else "")

    base = "".join(
        f'<path d="{poly(ring)}" fill="#fbfdfe" stroke="none"/>' for ring in peru.get("adm0", [])
    )
    regiones = "".join(
        f'<path d="{poly(ring)}" fill="none" stroke="#dde8f1" stroke-width="0.7"/>'
        for region in peru.get("regions", [])
        for ring in region.get("rings", [])
    )
    borde = "".join(
        f'<path d="{poly(ring)}" fill="none" stroke="#a9bfd0" stroke-width="1.3"/>'
        for ring in peru.get("adm0", [])
    )

    # Red vial: dos niveles de detalle. La departamental solo aparece al acercar,
    # si no el pais se convierte en una maraña a escala 1x.
    def road_group(lines: list, element_id: str, stroke: str, width: float,
                   opacity: float, hidden: bool) -> str:
        if not lines:
            return ""
        parts = []
        for line in lines:
            pts = " L ".join(f"{x:.1f},{y:.1f}" for x, y in (project(c[0], c[1]) for c in line))
            parts.append(f"M {pts}")
        estilo = ' style="display:none"' if hidden else ""
        return (
            f'<g id="{element_id}"{estilo}><path d="{" ".join(parts)}" fill="none" '
            f'stroke="{stroke}" stroke-opacity="{opacity}" stroke-width="{width}" '
            f'stroke-linejoin="round" vector-effect="non-scaling-stroke"/></g>'
        )

    vial = road_group(
        (roads or {}).get("departamental") or [], "vial-departamental",
        "#c3d2e0", 0.7, 0.9, True,
    ) + road_group(
        (roads or {}).get("nacional") or [], "vial-nacional",
        "#aebfd0", 0.9, 1.0, False,
    )

    # Alertas viales de SUTRAN: rombos, para no confundirlos con los puntos.
    alertas = []
    for a in alerts or []:
        lat, lng = a.get("lat"), a.get("lng")
        if lat is None or lng is None:
            continue
        x, y = project(lng, lat)
        if not (-6 <= x <= NAT_W + 6 and -6 <= y <= NAT_H + 6):
            continue
        colour = ALERT_COLOUR.get(a.get("estado_via"), "#8fa3b5")
        # El rombo crece con la severidad: interrumpido se ve más que normal.
        half = {"interrumpido": 4.6, "restringido": 3.7, "normal": 2.9}.get(
            a.get("estado_via"), 2.9
        )
        alertas.append(
            f'<rect class="amark" data-x="{x:.1f}" data-y="{y:.1f}" data-h="{half}" '
            f'x="{x - half:.1f}" y="{y - half:.1f}" width="{2 * half:.1f}" '
            f'height="{2 * half:.1f}" '
            f'transform="rotate(45 {x:.1f} {y:.1f})" fill="{colour}" stroke="#fff" '
            f'stroke-width="0.9" vector-effect="non-scaling-stroke">'
            f'<title>{html.escape(a.get("via_nombre") or a.get("titulo") or "")}'
            f' · {ALERT_LABEL.get(a.get("estado_via"), "?")} · '
            f'{html.escape(a.get("distrito") or "")}, {html.escape(a.get("region") or "")}'
            f"</title></rect>"
        )

    # Etiquetas: solo lo que alerta o destaca, para no tapar el mapa.
    top = sorted(rows, key=lambda r: -r["score"])[:5]
    label_ids = {r["id"] for r in rows if r["level"] != "Bajo"} | {r["id"] for r in top}
    label_ids |= {"tumbes", "tacna", "lima"}
    candidates = sorted(
        (r for r in rows if r["id"] in label_ids), key=lambda r: -r["score"]
    )

    placed: list[tuple[float, float]] = []
    labels = []
    for r in candidates:
        x, y = project(r["lon"], r["lat"])
        if any(abs(y - py) < 11 and abs(x - px) < 70 for px, py in placed):
            continue
        placed.append((x, y))
        # Si el punto está pegado al borde, la etiqueta va hacia adentro.
        if x < 72:
            anchor, dx = "start", 8
        elif x > NAT_W - 72:
            anchor, dx = "end", -8
        elif r["lon"] < -76.4:
            anchor, dx = "end", -7
        else:
            anchor, dx = "start", 7
        labels.append(
            f'<text class="mlabel" data-fs="10.5" data-a="{anchor}" '
            f'data-w="{len(r["name"]) * 5.8:.0f}" x="{x + dx:.1f}" y="{y + 3.4:.1f}" '
            f'font-size="10.5" font-weight="600" fill="#3b2a1c" stroke="#fff9e8" '
            f'stroke-width="2.8" '
            f'paint-order="stroke" text-anchor="{anchor}">'
            f"{html.escape(r['name'])}</text>"
        )

    # El tamaño del círculo crece con la lluvia prevista a 72 h (raíz cuadrada,
    # para que un evento de 70 mm no aplaste a los de 20 mm).
    peak72 = max((r["next72"] for r in rows), default=0.0) or 1.0
    markers = []
    for r in rows:
        x, y = project(r["lon"], r["lat"])
        colour = LEVEL_STYLE[r["level"]]["bar"]
        radius = 4.0 + 4.2 * math.sqrt(max(r["next72"], 0.0) / peak72)
        if r["id"] in PRIORITY_ZONES:
            markers.append(
                f'<circle class="cmark" data-r="{radius + 4.6:.1f}" cx="{x:.1f}" '
                f'cy="{y:.1f}" r="{radius + 4.6:.1f}" fill="none" stroke="{colour}" '
                f'stroke-width="1.3" stroke-dasharray="3 2.2" vector-effect="non-scaling-stroke"/>'
            )
        markers.append(
            f'<circle class="cmark" data-r="{radius:.1f}" cx="{x:.1f}" cy="{y:.1f}" '
            f'r="{radius:.1f}" fill="{colour}" stroke="#fff" stroke-width="1.6" '
            f'vector-effect="non-scaling-stroke"><title>{html.escape(r["name"])} '
            f'({html.escape(r["dept"])}): '
            f'{r["level"]} · {fmt(r["next24"])} mm/24 h · {fmt(r["next72"])} mm/72 h'
            f'{" · Productos SISAP: " + html.escape(", ".join(r.get("sisap_products", []))) if r.get("sisap_products") else ""}'
            f'</title></circle>'
        )

    bar_px = 200 * scale / 111.32
    scale_bar = (
        f'<line x1="18" y1="{NAT_H - 22}" x2="{18 + bar_px:.1f}" y2="{NAT_H - 22}" '
        f'stroke="#5b7186" stroke-width="2"/>'
        f'<line x1="18" y1="{NAT_H - 26}" x2="18" y2="{NAT_H - 18}" stroke="#5b7186" stroke-width="1.5"/>'
        f'<line x1="{18 + bar_px:.1f}" y1="{NAT_H - 26}" x2="{18 + bar_px:.1f}" '
        f'y2="{NAT_H - 18}" stroke="#5b7186" stroke-width="1.5"/>'
        f'<text x="18" y="{NAT_H - 30}" font-size="9" fill="#5b7186">200 km</text>'
    )

    svg = (
        f'<svg viewBox="0 0 {NAT_W} {NAT_H}" xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Mapa del Perú con los puntos de alerta">'
        f'<rect width="{NAT_W}" height="{NAT_H}" fill="#f4eddf" rx="10"/>'
        + base
        + regiones
        + vial
        + borde
        + f'<g id="capa-carreteras">{"".join(alertas)}</g>'
        + f'<g id="capa-cuencas">{"".join(markers)}{"".join(labels)}</g>'
        + scale_bar
        + "</svg>"
    )

    return svg


def _icon_cuenca() -> str:
    return (
        '<svg width="15" height="15" viewBox="0 0 15 15" aria-hidden="true">'
        '<circle cx="7.5" cy="7.5" r="5.2" fill="#d9a520" stroke="#fff" stroke-width="1.4"/>'
        "</svg>"
    )


def _icon_alerta() -> str:
    return (
        '<svg width="15" height="15" viewBox="0 0 15 15" aria-hidden="true">'
        '<rect x="3.6" y="3.6" width="7.8" height="7.8" transform="rotate(45 7.5 7.5)" '
        'fill="#c0392b" stroke="#fff" stroke-width="1.1"/>'
        "</svg>"
    )


def _icon_zona() -> str:
    return (
        '<svg width="15" height="15" viewBox="0 0 15 15" aria-hidden="true">'
        '<circle cx="7.5" cy="7.5" r="5.6" fill="none" stroke="#2e9e63" stroke-width="1.3" '
        'stroke-dasharray="2.5 2"/>'
        "</svg>"
    )


def _icon_vial() -> str:
    return (
        '<svg width="15" height="15" viewBox="0 0 15 15" aria-hidden="true">'
        '<path d="M1.5 11.5 L7.5 3.5 L13.5 11.5" fill="none" stroke="#b3c4d4" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
        "</svg>"
    )


def render_map_controls(rows: list[dict], alerts: list[dict]) -> str:
    """Botones de capa del mapa nacional."""
    en_vigilancia = sum(1 for r in rows if r["level"] != "Bajo")
    n_alertas = len(alerts or [])
    return (
        '<div class="layers"><span class="layers-lbl">Capas</span>'
        '<button type="button" class="tgl on" data-capa="capa-cuencas" aria-pressed="true" '
        'title="Puntos de monitoreo coloreados por nivel de activación">'
        '<span class="sw sw-cir"></span>Activación de cuencas<b>'
        f'{en_vigilancia}</b></button>'
        '<button type="button" class="tgl on" data-capa="capa-carreteras" aria-pressed="true" '
        'title="Alertas viales activas de SUTRAN">'
        '<span class="sw sw-dia"></span>Alertas de carreteras<b>'
        f'{n_alertas}</b></button>'
        '<div class="zoomctl">'
        '<button type="button" class="zbtn" data-zoom="out" title="Alejar" '
        'aria-label="Alejar">\u2212</button>'
        '<button type="button" class="zbtn" data-zoom="in" title="Acercar" '
        'aria-label="Acercar">+</button>'
        '<button type="button" class="zbtn" data-zoom="reset" title="Ver todo el pa\u00eds">'
        "Todo</button>"
        '<span class="zlevel" id="zoom-nivel">1,0\u00d7</span>'
        "</div>"
        "</div>"
    )


def _dot(size: float, colour: str) -> str:
    return (
        '<svg width="15" height="15" viewBox="0 0 15 15" aria-hidden="true">'
        f'<circle cx="7.5" cy="7.5" r="{size}" fill="{colour}" stroke="#fff" stroke-width="1.2"/>'
        "</svg>"
    )


def _dia(size: float, colour: str) -> str:
    return (
        '<svg width="15" height="15" viewBox="0 0 15 15" aria-hidden="true">'
        f'<rect x="{7.5 - size:.1f}" y="{7.5 - size:.1f}" width="{2 * size:.1f}" '
        f'height="{2 * size:.1f}" transform="rotate(45 7.5 7.5)" fill="{colour}" '
        'stroke="#fff" stroke-width="1"/>'
        "</svg>"
    )


def render_symbol_legend() -> str:
    """Leyenda con cada variante de marcador, no un ejemplar genérico."""
    niveles = "".join(
        f'<span class="ml-i">{_dot(4.4, LEVEL_STYLE[n]["bar"])}{n}</span>'
        for n in ("Bajo", "Medio", "Alto", "Muy alto")
    )
    vial = "".join(
        f'<span class="ml-i">{_dia(s, ALERT_COLOUR[k])}{ALERT_LABEL[k]}</span>'
        for k, s in (("interrumpido", 4.6), ("restringido", 3.6), ("normal", 2.8))
    )
    return (
        '<div class="mlegend"><div class="ml-title">Cómo leer el mapa</div>'
        '<div class="ml-groups">'
        '<div class="ml-group">'
        '<div class="ml-gtitle">Activación de cuencas</div>'
        f'<div class="ml-items ml-levels">{niveles}</div>'
        '<div class="ml-note">Círculo. El tamaño crece con la lluvia prevista a 72 h.</div>'
        "</div>"
        '<div class="ml-group">'
        '<div class="ml-gtitle">Alertas viales · SUTRAN</div>'
        f'<div class="ml-items">{vial}</div>'
        '<div class="ml-note">Rombo. El tamaño crece con la severidad del corte.</div>'
        "</div>"
        '<div class="ml-group">'
        '<div class="ml-gtitle">Referencias</div>'
        '<div class="ml-items">'
        f'<span class="ml-i">{_icon_zona()}Zona priorizada</span>'
        f'<span class="ml-i">{_icon_vial()}Red vial</span>'
        "</div>"
        '<div class="ml-note">Anillo punteado: tiene mapa de quebradas en detalle.</div>'
        "</div>"
        "</div></div>"
    )


def render_alert_side(
    rows: list[dict], geodata: dict | None = None, road_alerts: list[dict] | None = None
) -> str:
    """Columna derecha del mapa nacional: quién está en vigilancia."""
    alerts = [r for r in rows if r["level"] != "Bajo"]
    if alerts:
        titulo = f"{len(alerts)} punto{'s' if len(alerts) != 1 else ''} en vigilancia"
        items = alerts
    else:
        titulo = "Sin puntos sobre el umbral de Medio"
        items = sorted(rows, key=lambda r: -r["score"])[:5]

    lis = []
    for r in items:
        lis.append(
            f'<li><i style="background:{LEVEL_STYLE[r["level"]]["bar"]}"></i>'
            f'<span><span class="nm">{html.escape(r["name"])}</span>'
            f'<div class="mt">{html.escape(r["dept"])} · {fmt(r["next72"])} mm/72 h</div></span>'
            f'<span class="sp">{chip(r["level"])}</span></li>'
        )
    nota = (
        ""
        if alerts
        else '<div class="natnota">Se listan los cinco de mayor índice. '
        "Cualquier punto puede activarse cuando el pronóstico cambie.</div>"
    )

    zonas = ""
    if geodata:
        rank = {"Bajo": 0, "Medio": 1, "Alto": 2, "Muy alto": 3}
        filas = []
        for zone in geodata.values():
            ids = set(zone.get("points") or [])
            pts = [r for r in rows if r["id"] in ids]
            if not pts:
                continue
            worst = max(pts, key=lambda r: rank[r["level"]])
            filas.append(
                f'<li><i style="background:{LEVEL_STYLE[worst["level"]]["bar"]}"></i>'
                f'<span><span class="nm">{html.escape(zone.get("label", ""))}</span>'
                f'<div class="mt">{html.escape(zone.get("cuenca", ""))} · '
                f'{len(zone.get("features", []))} tramos</div></span>'
                f'<span class="sp">{chip(worst["level"])}</span></li>'
            )
        if filas:
            zonas = (
                '<h4 style="margin-top:20px">Zonas con mapa de quebradas</h4>'
                f'<ul class="alist">{"".join(filas)}</ul>'
            )

    alertas_html = ""
    if road_alerts:
        counts = {k: sum(1 for a in road_alerts if a.get("estado_via") == k) for k in ALERT_COLOUR}
        items = "".join(
            f'<span><i class="dm" style="background:{ALERT_COLOUR[k]}"></i>'
            f"{ALERT_LABEL[k]}<b> {counts[k]}</b></span>"
            for k in ("interrumpido", "restringido", "normal")
        )
        alertas_html = (
            '<h4 style="margin-top:20px">Alertas viales · SUTRAN</h4>'
            f'<div class="aleg">{items}</div>'
        )

    return (
        f'<div class="natside"><h4>{titulo}</h4><ul class="alist">{"".join(lis)}</ul>{nota}'
        f"{zonas}"
        f"{alertas_html}"
        '<div class="natnota">El anillo punteado del mapa marca estas tres zonas.</div>'
        "</div>"
    )


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _nearest(feature: dict, rows: list[dict]) -> dict:
    mid = feature["coords"][len(feature["coords"]) // 2]
    return min(rows, key=lambda r: _haversine_km(mid[1], mid[0], r["lat"], r["lon"]))


def _panel(zone: dict, rows: list[dict]) -> str:
    feats = zone.get("features") or []
    ids = set(zone.get("points") or [])
    rows_zone = [r for r in rows if r["id"] in ids]
    if not feats or not rows_zone:
        return ""

    # Encuadre: el area de interes de la zona, no la extension de los rios
    # completos (Overpass devuelve vias enteras y estirarian el mapa).
    bbox = zone.get("bbox")
    if bbox:
        south, west, north, east = bbox
        lons = [west, east] + [r["lon"] for r in rows_zone]
        lats = [south, north] + [r["lat"] for r in rows_zone]
    else:
        lons = [c[0] for f in feats for c in f["coords"]] + [r["lon"] for r in rows_zone]
        lats = [c[1] for f in feats for c in f["coords"]] + [r["lat"] for r in rows_zone]
    lon0, lon1, lat0, lat1 = min(lons), max(lons), min(lats), max(lats)
    kx = math.cos(math.radians((lat0 + lat1) / 2))
    span_x = max((lon1 - lon0) * kx, 1e-6)
    span_y = max(lat1 - lat0, 1e-6)
    scale = min((MAP_W - 2 * MAP_PAD) / span_x, (MAP_H - 2 * MAP_PAD) / span_y)
    off_x = (MAP_W - span_x * scale) / 2
    off_y = (MAP_H - span_y * scale) / 2

    def project(lon: float, lat: float) -> tuple[float, float]:
        return (off_x + (lon - lon0) * kx * scale, off_y + (lat1 - lat) * scale)

    halos, strokes = [], []
    for feat in sorted(feats, key=lambda f: _KIND_ORDER.get(f["kind"], 2)):
        owner = _nearest(feat, rows_zone)
        colour = LEVEL_STYLE[owner["level"]]["bar"]
        alpha = 0.88 if owner["level"] == "Bajo" else 1.0
        width = _KIND_WIDTH.get(feat["kind"], 1.0)
        pts = " L ".join(f"{x:.1f},{y:.1f}" for x, y in (project(c[0], c[1]) for c in feat["coords"]))
        path = f"M {pts}"
        # Halo del color del fondo: separa cauces que se cruzan sin tapar el trazo.
        halos.append(
            f'<path d="{path}" fill="none" stroke="#f4f8fb" stroke-width="{width + 2.6}" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
        )
        strokes.append(
            f'<path d="{path}" fill="none" stroke="{colour}" stroke-opacity="{alpha}" '
            f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round">'
            f"<title>{html.escape(feat['name'])} · {owner['name']} · {owner['level']} "
            f"({fmt(owner['next72'])} mm/72 h)</title></path>"
        )

    markers = []
    for r in rows_zone:
        x, y = project(r["lon"], r["lat"])
        anchor = "start" if r["lon"] < lon0 + (lon1 - lon0) * 0.55 else "end"
        dx = 6 if anchor == "start" else -6
        markers.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{LEVEL_STYLE[r["level"]]["bar"]}" '
            f'stroke="#fff" stroke-width="1.6"><title>{html.escape(r["name"])}: '
            f'{r["level"]} · {fmt(r["next72"])} mm/72 h</title></circle>'
            f'<text x="{x + dx:.1f}" y="{y + 3.3:.1f}" font-size="10" font-weight="600" '
            f'fill="#3b2a1c" stroke="#fff9e8" stroke-width="2.4" paint-order="stroke" '
            f'text-anchor="{anchor}">{html.escape(r["name"])}</text>'
        )

    bar_px = 5 * scale / 111.32
    scale_bar = (
        f'<line x1="14" y1="285" x2="{14 + bar_px:.1f}" y2="285" stroke="#64748b" stroke-width="2"/>'
        f'<line x1="14" y1="281" x2="14" y2="289" stroke="#64748b" stroke-width="1.5"/>'
        f'<line x1="{14 + bar_px:.1f}" y1="281" x2="{14 + bar_px:.1f}" y2="289" '
        f'stroke="#64748b" stroke-width="1.5"/>'
        f'<text x="14" y="278" font-size="8.5" fill="#64748b">5 km</text>'
    )

    # Nivel de la zona = el mas alto de sus puntos de control.
    rank = {"Bajo": 0, "Medio": 1, "Alto": 2, "Muy alto": 3}
    worst = max(rows_zone, key=lambda r: rank[r["level"]])

    cauces = []
    seen: dict[str, dict] = {}
    for feat in feats:
        seen.setdefault(feat["name"], feat)
    for name in sorted(seen):
        owner = _nearest(seen[name], rows_zone)
        cauces.append(
            f'<span><i style="background:{LEVEL_STYLE[owner["level"]]["bar"]}"></i>{html.escape(name)}</span>'
        )

    return (
        '<div class="zmap">'
        f'<h3>{html.escape(zone["label"])}</h3>'
        f'<div class="sub"><span>{html.escape(zone.get("cuenca", ""))} · '
        f'{len(seen)} cauces</span> {chip(worst["level"])}</div>'
        f'<svg viewBox="0 0 {MAP_W} {MAP_H}" xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Ríos y quebradas de {html.escape(zone["label"])}">'
        f'<rect width="{MAP_W}" height="{MAP_H}" fill="#f4eddf" rx="7"/>'
        + "".join(halos)
        + "".join(strokes)
        + "".join(markers)
        + scale_bar
        + f'<rect x="0.5" y="0.5" width="{MAP_W - 1}" height="{MAP_H - 1}" fill="none" '
        f'stroke="#dbe4ec" rx="7"/>'
        + "</svg>"
        f'<div class="cauces">{"".join(cauces)}</div>'
        f'<div class="znota">{html.escape(zone.get("nota", ""))}</div>'
        "</div>"
    )


def render_map_card(rows: list[dict], geodata: dict) -> str:
    if not geodata:
        return ""
    panels = "".join(_panel(zone, rows) for zone in geodata.values())
    if not panels:
        return ""
    return (
        '<div class="nota-sec">Trazado real de OpenStreetMap. El color indica el nivel del '
        "punto de monitoreo más cercano a cada cauce; <strong>no</strong> es una predicción "
        "por tramo. Con celdas de 0,25° las quebradas de una misma zona comparten señal.</div>"
        f'<div class="maps">{panels}</div>' + level_legend()
    )


def alerts_hint(alerts: list[dict]) -> str:
    """Resumen corto para el encabezado plegable."""
    if not alerts:
        return "sin alertas activas"
    cuenta = {
        "interrumpido": sum(1 for a in alerts if a.get("estado_via") == "interrumpido"),
        "restringido": sum(1 for a in alerts if a.get("estado_via") == "restringido"),
        "normal": sum(1 for a in alerts if a.get("estado_via") == "normal"),
    }
    return (
        f'{cuenta["interrumpido"]} interrumpidas · '
        f'{cuenta["restringido"]} restringidas · '
        f'{cuenta["normal"]} normales'
    )


def render_alerts_card(alerts: list[dict]) -> str:
    """Contenido de la tabla de alertas viales activas de SUTRAN."""
    if not alerts:
        return ""
    n_nat = sum(1 for a in alerts if a.get("motivo") == "fenomenos_naturales")
    resumen = "".join(
        f'<span class="pill-dark"><span class="dot" style="background:{ALERT_COLOUR[k]}">'
        f"</span>{sum(1 for a in alerts if a.get('estado_via') == k)} {txt}</span>"
        for k, txt in (
            ("interrumpido", "interrumpidas"),
            ("restringido", "restringidas"),
            ("normal", "normales"),
        )
    ) + f'<span class="pill-dark">{n_nat} por fenómenos naturales</span>'

    body = []
    for a in alerts:
        estado = a.get("estado_via")
        colour = ALERT_COLOUR.get(estado, "#2e9e63")
        motivo = (a.get("motivo") or "").replace("_", " ")
        motivo = motivo[:1].upper() + motivo[1:]
        via = a.get("via_nombre") or a.get("via_codigo") or "—"
        km = a.get("km") or ""
        detail = " · ".join(
            x for x in [a.get("descripcion"), km, a.get("vigente_hasta") and f"hasta {a['vigente_hasta']}"] if x
        )
        km_html = f' <span class="km">{html.escape(km)}</span>' if km else ""
        body.append(
            f'<tr title="{html.escape(detail[:200])}">'
            f'<td><span class="dot" style="background:{colour}"></span>'
            f'{ALERT_LABEL.get(estado, "?")}</td>'
            f'<td><span class="via">{html.escape(via)}</span>{km_html}</td>'
            f'<td>{html.escape(a.get("distrito") or "—")}, '
            f'{html.escape(a.get("region") or "—")}</td>'
            f'<td>{html.escape(motivo)}</td>'
            f'<td>{html.escape(a.get("situacion") or "")}</td>'
            "</tr>"
        )

    return (
        '<div class="subtitle">Estado de las vías nacionales según el '
        '<a href="https://alertasviales.sutran.gob.pe/">Mapa Interactivo de Alertas</a> '
        "de SUTRAN. Ordenadas por estado de la vía y, dentro de cada grupo, los "
        "fenómenos naturales primero.</div>"
        f'<div class="chips">{resumen}</div>'
        '<div class="scroll"><table><thead><tr>'
        "<th>Estado</th><th>Vía</th><th>Lugar</th><th>Motivo</th><th>Situación</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
    )


def render_nino_line(nino: dict) -> str:
    """Franja con el estado oficial de El Niño Costero (SENAMHI/ENFEN)."""
    alerta = (nino or {}).get("alerta") or {}
    if not alerta:
        return ""
    nombre = alerta.get("nombreEvento") or "El Niño"
    magnitud = (alerta.get("magnitud") or "").capitalize()
    vigencia = alerta.get("vigencia") or ""
    escenarios = " · ".join(
        f'{p.get("texto")} ({p.get("subtexto")})'
        for p in (nino.get("pronostico") or [])[:3]
        if p.get("texto")
    )
    extra = f'<span class="nesc">Escenarios: {html.escape(escenarios)}</span>' if escenarios else ""
    return (
        '<div class="nino-line">'
        '<span class="nk">Contexto El Niño</span>'
        f'<span class="nt">{html.escape(nombre)} · magnitud '
        f"<strong>{html.escape(magnitud)}</strong> · {html.escape(vigencia)}</span>"
        f"{extra}</div>"
    )


def render_verification_card(verif: list[dict]) -> str:
    """Resultados de POD/FAR/CSI del umbral p95 evaluado sobre 1991-2020."""
    if not verif:
        return ""

    def barra(valor: float | None, color: str) -> str:
        ancho = 0 if valor is None else max(0.0, min(valor, 1.0)) * 100
        texto = "—" if valor is None else f"{valor:.2f}"
        return (
            f'<div class="vcell"><span class="vbar"><i style="width:{ancho:.0f}%;'
            f'background:{color}"></i></span><span class="vnum">{texto}</span></div>'
        )

    filas = []
    for v in verif:
        a, b, c = v["hits"], v["misses"], v["false_alarms"]
        pod = a / (a + b) if (a + b) else None
        far = c / (a + c) if (a + c) else None
        csi = a / (a + b + c) if (a + b + c) else None
        if far is not None and far > 0.6:
            juicio = '<span class="vtag mal">poca precisión</span>'
        elif pod is not None and pod < 0.5:
            juicio = '<span class="vtag mal">pierde eventos</span>'
        else:
            juicio = '<span class="vtag ok">aceptable</span>'
        filas.append(
            f'<tr><td class="num">≥ {v["umbral"]:.0f} mm</td>'
            f"<td>{barra(pod, '#2e9e63')}</td>"
            f"<td>{barra(far, '#c0392b')}</td>"
            f"<td class=\"num\">{csi:.2f}</td>"
            f'<td class="num">{a + b}</td><td class="num">{a + c}</td>'
            f"<td>{juicio}</td></tr>"
        )

    return (
        '<div class="card"><h2>Verificación de la regla de disparo</h2>'
        '<div class="subtitle">¿El umbral p95 sirve como alarma? Se evalúa sobre 1991–2020 '
        "en los 21 puntos, asumiendo <strong>prognosis perfecta</strong>: mide si el umbral "
        "está bien puesto, no la habilidad del pronóstico.</div>"
        '<div class="scroll"><table><thead><tr>'
        '<th class="num">Evento</th><th>POD</th><th>FAR</th>'
        '<th class="num">CSI</th><th class="num">Eventos</th><th class="num">Alarmas</th>'
        "<th>Lectura</th></tr></thead><tbody>" + "".join(filas) + "</tbody></table></div>"
        '<div class="note" style="margin-top:14px">Con el umbral p95, un día de 50 mm se '
        "detecta siempre, pero <strong>el 92% de las alarmas no tienen evento</strong>: "
        "para cada día realmente severo se disparan unas doce alertas. El umbral solo no "
        "basta; hay que combinarlo con la probabilidad del ensemble y la intensidad horaria, "
        "que es justo lo que ya hace el índice.</div></div>"
    )


def section(title: str, hint: str, inner: str, start_open: bool = False) -> str:
    """Una sección plegable del tablero."""
    return (
        f'<details class="sec"{" open" if start_open else ""}>'
        f'<summary><span class="st">{title}</span><span class="sh">{hint}</span></summary>'
        f'<div class="sbody">{inner}</div></details>'
    )


def card_block(title: str, hint: str, inner: str) -> str:
    """Una sección siempre visible, con el mismo encabezado que las plegables."""
    return (
        f'<div class="card"><h2>{title}</h2>'
        f'<div class="subtitle">{hint}</div>'
        f"{inner}</div>"
    )


def build_html(
    rows,
    dates,
    meta,
    run_stamp,
    geodata=None,
    peru=None,
    roads=None,
    alerts=None,
    nino=None,
    verif=None,
) -> str:
    costa = [r for r in rows if r["rol"] == "Costa"]
    alta = [r for r in rows if r["rol"] != "Costa"]
    total = len(rows)
    n_cells = len({(math.floor(r["lat"] / 0.25), math.floor(r["lon"] / 0.25)) for r in rows})
    peak = max(rows, key=lambda r: r["next72"]) if rows else None
    scale72 = max((r["next72"] for r in rows), default=0.0) or 1.0
    n_rain = sum(1 for r in rows if r["next72"] >= 1.0)
    n_alert = sum(1 for r in rows if r["level"] in ("Alto", "Muy alto"))
    max_prob = max((r["prob25"] for r in rows), default=0.0)

    if n_alert:
        estado = (
            f'<strong>{n_alert} punto(s) en nivel Alto o superior.</strong> '
            "Revisar la tabla de detalle y contrastar con los avisos del SENAMHI."
        )
    elif n_rain:
        estado = (
            f"Sin puntos en nivel Alto. {n_rain} de {total} puntos prevén lluvia "
            "apreciable (&ge;1 mm en 72 h); el resto permanece seco."
        )
    else:
        estado = (
            f"Ningún punto prevé lluvia apreciable en 72 h. Escenario propio de la "
            "temporada seca; el tablero sigue siendo válido como vigilancia."
        )

    kmeta = "".join(
        f'<span class="pill">{html.escape(k)}: {html.escape(str(v))}</span>' for k, v in meta
    )

    national = render_national_map(rows, peru or {}, roads or {}, alerts or [])
    side = render_alert_side(rows, geodata or {}, alerts or [])
    alerts_card = (
        section(
            "Alertas viales · SUTRAN",
            alerts_hint(alerts or []),
            render_alerts_card(alerts or []),
        )
        if alerts
        else ""
    )
    nino_line = render_nino_line(nino or {})
    controls = render_map_controls(rows, alerts or [])
    legend = render_symbol_legend()
    verif_card = render_verification_card(verif or [])

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Monitor de lluvia y riesgo de huaicos · Costa peruana</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

<nav class="nav"><a class="active" href="index.html">Clima y riesgo</a>
  <a href="precios.html">Precios SISAP</a>
  <a href="abastecimiento.html">Riesgo de abastecimiento</a>
  <a href="puertos.html">Puertos y mar</a></nav>

<header class="hero">
  <h1>Monitor de lluvia y riesgo de huaicos · Costa peruana</h1>
  <p>Vigilancia de precipitación en 21 puntos de la costa y sus cuencas altas.
     Pronóstico determinístico multimodelo, probabilidad por ensemble y
     comparación contra la climatología 1991–2020.</p>
  <div class="hero-meta">{kmeta}</div>
</header>

<div class="kpis">
  <div class="kpi"><div class="label">Puntos monitoreados</div>
    <div class="value">{total}</div>
    <div class="sub">{len(costa)} costa · {len(alta)} cuenca alta</div></div>
  <div class="kpi"><div class="label">Máx. acumulado 72 h</div>
    <div class="value">{fmt(peak["next72"]) if peak else "—"}<span style="font-size:14px"> mm</span></div>
    <div class="sub">{html.escape(peak["name"]) if peak else "—"}</div></div>
  <div class="kpi"><div class="label">Puntos con lluvia ≥1 mm</div>
    <div class="value">{n_rain}</div>
    <div class="sub">en ventana de 72 h</div></div>
  <div class="kpi"><div class="label">Nivel Alto o superior</div>
    <div class="value">{n_alert}</div>
    <div class="sub">índice heurístico v0</div></div>
  <div class="kpi"><div class="label">Máx. P(≥25 mm/24 h)</div>
    <div class="value">{fmt(max_prob * 100, 0)}<span style="font-size:14px"> %</span></div>
    <div class="sub">ensemble ECMWF (50 miembros)</div></div>
</div>

<div class="card">
  <h2>Lectura del día</h2>
  <div class="note">{estado}</div>
  {nino_line}
</div>

<div class="card">
  <h2>Mapa nacional de alertas</h2>
  <div style="font-size:12px;color:#64748b;margin:-6px 0 12px">
    Los {total} puntos de monitoreo sobre la red vial nacional. Arrastra para mover,
    usa la rueda o los botones para acercar: al llegar a 2× aparece la red departamental.
    El símbolo de cuenca es un círculo y el de alerta vial, un rombo.
  </div>
  {controls}
  <div class="natwrap">
    <div class="natmap">{national}{legend}</div>
    {side}
  </div>
</div>

{alerts_card}

{card_block("Transecto costero · norte a sur",
            "Acumulado previsto en 72 h por punto. El color indica el nivel de riesgo.",
            render_transect(costa, "", "", scale72))}
{card_block("Cuencas altas aportantes",
            "Donde nace el agua que puede activar quebradas y generar huaicos o aluviones.",
            render_transect(alta, "", "", scale72))}
{section("Mapas de quebradas por zona", "Chosica · Piura · Tumbes · geometría OSM",
         render_map_card(rows, geodata or {}))}
{card_block("Pronóstico diario · 16 días",
            "Precipitación diaria del modelo de referencia (ECMWF IFS), en mm. "
            "Las celdas vacías son días sin lluvia prevista.",
            render_heatmap(sorted(rows, key=lambda r: -r["lat"]), dates))}
{card_block("Detalle por punto",
            "Acumulados, intensidad máxima horaria, umbrales climatológicos (1 y 3 días), "
            "probabilidad del ensemble y dispersión entre modelos.",
            render_table(rows))}

{verif_card}

<details class="sec">
  <summary><span class="st">Metodología y límites</span>
    <span class="sh">Cómo se calcula y hasta dónde llega</span></summary>
  <div class="sbody">
  <details open>
    <summary>Índice de riesgo (v0, heurístico)</summary>
    <p><code>score = {W_VOLUME}·V + {W_INTENSITY}·I + {W_PROB}·P</code> donde<br>
    <code>R = max(24h / max(p95_1d, {FLOOR_MM_24H:.0f}), 72h / max(p95_3d, {FLOOR_MM_72H:.0f}))</code><br>
    <code>V = min(R / 2, 1)</code> · <code>I = min(máx_mm_h / {INTENSITY_REF:.0f}, 1)</code> ·
    <code>P = P(≥25 mm en 24 h)</code> del ensemble ECMWF.<br>
    Cortes: Bajo &lt; 0,20 · Medio 0,20–0,38 · Alto 0,38–0,55 · Muy alto ≥ 0,55.</p>
    <p>Estar justo en el p95 climatológico equivale a V = 0,5: ya es un día notable,
    no un día normal. Duplicar ese umbral satura la componente en 1,0. Se usan dos
    escalas de volumen porque el disparador no es solo el día extremo: tres días de
    lluvia moderada saturan el suelo y activan una quebrada igual que un solo aguacero.</p>
    <p><strong>Esto no es un modelo entrenado.</strong> Es una heurística transparente
    para ordenar la vigilancia y establecer la línea base contra la que se medirá
    el modelo de clasificación con LightGBM en la siguiente fase.</p>
  </details>
  <details>
    <summary>Por qué mirar cuencas altas y no solo la costa</summary>
    <p>En la costa peruana la lluvia directa es escasa: el peligro real es que
    llueva fuerte en la cuenca alta y el agua baje por una quebrada como huaico o
    aluvión. Por eso la red incluye Huancabamba, Santa Eulalia y Yauyos, y el
    indicador clave es la intensidad (mm/h) más la humedad antecedente del suelo,
    no solo el acumulado diario.</p>
  </details>
  <details>
    <summary>Límites conocidos</summary>
    <ul>
      <li><strong>Resolución efectiva:</strong> los {total} puntos caen en solo
          <strong>{n_cells} celdas distintas</strong> del retículo de 0,25° (~28 km).
          Los pares que comparten celda —Chosica y Santa Eulalia, por ejemplo—
          muestran valores idénticos: no es un error, es el límite del modelo.</li>
      <li>La costa es una franja de 50–150 km, así que el modelo global subestima
          la lluvia orográfica y convectiva de las quebradas.</li>
      <li>Los umbrales p95/p99 provienen de ERA5, que suaviza extremos convectivos.
          Sirven para ordenar, no como valor absoluto de diseño.</li>
      <li>Sin validación contra observaciones de SENAMHI todavía: esa es la fase 2.</li>
      <li>Un solo miembro determinístico no comunica riesgo; por eso se reporta
          siempre la probabilidad del ensemble junto al valor puntual.</li>
    </ul>
  </details>
  <details>
    <summary>Fuentes y próximos pasos</summary>
    <p>Datos: ECMWF IFS, NOAA GFS, DWD ICON y ensemble ECMWF vía
    <a href="https://open-meteo.com/">Open-Meteo</a>; climatología ERA5 vía Open-Meteo
    Archive. Estado ENSO: <a href="https://enfen.imarpe.gob.pe/">ENFEN</a>.</p>
    <p>Siguiente fase: (1) ingesta de estaciones SENAMHI y GPM IMERG para calibrar;
    (2) verificación POD/FAR/CSI por umbral contra 2017 y 2023; (3) clasificador
    LightGBM con humedad antecedente y forzantes ENSO; (4) mapa de susceptibilidad
    INGEMMET cruzado con exposición de CENEPRED.</p>
  </details>
  </div>
</details>

<footer>
  Generado {run_stamp} · Datos meteorológicos por
  <a href="https://open-meteo.com/">Open-Meteo.com</a> (CC BY 4.0) ·
  Climatología ERA5 (Copernicus/C3S) · Prototipo de investigación, no apto para
  decisiones de emergencia sin validación oficial.
</footer>

</div>

<script>
(function () {{
  var btns = document.querySelectorAll('.tgl[data-capa]');
  for (var i = 0; i < btns.length; i++) {{
    btns[i].addEventListener('click', function () {{
      var capa = document.getElementById(this.getAttribute('data-capa'));
      var apagada = this.classList.toggle('off');
      this.classList.toggle('on', !apagada);
      this.setAttribute('aria-pressed', apagada ? 'false' : 'true');
      if (capa) {{ capa.style.display = apagada ? 'none' : ''; }}
    }});
  }}

  var svg = document.querySelector('.natmap > svg');
  if (!svg || !svg.viewBox) return;
  var vb = svg.viewBox.baseVal;
  var home = {{ x: vb.x, y: vb.y, w: vb.width }};
  var ratio = vb.height / vb.width;
  var minW = home.w / 12;
  var cur = {{ x: home.x, y: home.y, w: home.w }};
  var nivel = document.getElementById('zoom-nivel');
  var dep = document.getElementById('vial-departamental');

  function escalar(z) {{
    var k = 1 / Math.pow(z, 0.6), j;
    var circ = svg.querySelectorAll('.cmark');
    for (j = 0; j < circ.length; j++) {{
      circ[j].setAttribute('r', (parseFloat(circ[j].getAttribute('data-r')) * k).toFixed(2));
    }}
    var rom = svg.querySelectorAll('.amark');
    for (j = 0; j < rom.length; j++) {{
      var x = parseFloat(rom[j].getAttribute('data-x'));
      var y = parseFloat(rom[j].getAttribute('data-y'));
      var h = parseFloat(rom[j].getAttribute('data-h')) * k;
      rom[j].setAttribute('x', (x - h).toFixed(2));
      rom[j].setAttribute('y', (y - h).toFixed(2));
      rom[j].setAttribute('width', (2 * h).toFixed(2));
      rom[j].setAttribute('height', (2 * h).toFixed(2));
    }}
    var etq = svg.querySelectorAll('.mlabel');
    for (j = 0; j < etq.length; j++) {{
      var el = etq[j];
      el.setAttribute('font-size', (parseFloat(el.getAttribute('data-fs')) * k).toFixed(2));
      var lx = parseFloat(el.getAttribute('x'));
      var ancho = parseFloat(el.getAttribute('data-w')) * k;
      var izq = el.getAttribute('data-a') === 'end' ? lx - ancho : lx;
      var der = el.getAttribute('data-a') === 'end' ? lx : lx + ancho;
      var dentro = izq > cur.x + 2 && der < cur.x + cur.w - 2;
      el.style.display = dentro ? '' : 'none';
    }}
  }}

  function aplicar() {{
    svg.setAttribute('viewBox', cur.x.toFixed(2) + ' ' + cur.y.toFixed(2) + ' ' +
      cur.w.toFixed(2) + ' ' + (cur.w * ratio).toFixed(2));
    var z = home.w / cur.w;
    if (nivel) {{ nivel.textContent = z.toFixed(1).replace('.', ',') + '\u00d7'; }}
    if (dep) {{ dep.style.display = z >= 2 ? '' : 'none'; }}
    escalar(z);
  }}

  function limitar() {{
    var h = cur.w * ratio;
    cur.x = Math.min(Math.max(cur.x, home.x), home.x + home.w - cur.w);
    cur.y = Math.min(Math.max(cur.y, home.y), home.y + home.w * ratio - h);
  }}

  function acercar(factor, cx, cy) {{
    var nw = Math.min(Math.max(cur.w * factor, minW), home.w);
    var k = nw / cur.w;
    cur.x = cx - (cx - cur.x) * k;
    cur.y = cy - (cy - cur.y) * k;
    cur.w = nw;
    limitar();
    aplicar();
  }}

  svg.addEventListener('wheel', function (ev) {{
    ev.preventDefault();
    var r = svg.getBoundingClientRect();
    var cx = cur.x + ((ev.clientX - r.left) / r.width) * cur.w;
    var cy = cur.y + ((ev.clientY - r.top) / r.height) * cur.w * ratio;
    acercar(ev.deltaY > 0 ? 1.2 : 1 / 1.2, cx, cy);
  }}, {{ passive: false }});

  var arrastre = null;
  svg.addEventListener('pointerdown', function (ev) {{
    arrastre = {{ x: ev.clientX, y: ev.clientY, cx: cur.x, cy: cur.y }};
    try {{ svg.setPointerCapture(ev.pointerId); }} catch (e) {{}}
  }});
  svg.addEventListener('pointermove', function (ev) {{
    if (!arrastre) return;
    var r = svg.getBoundingClientRect();
    cur.x = arrastre.cx - (ev.clientX - arrastre.x) * (cur.w / r.width);
    cur.y = arrastre.cy - (ev.clientY - arrastre.y) * (cur.w * ratio / r.height);
    limitar();
    aplicar();
  }});
  function soltar() {{ arrastre = null; }}
  svg.addEventListener('pointerup', soltar);
  svg.addEventListener('pointercancel', soltar);
  svg.addEventListener('pointerleave', soltar);

  var zb = document.querySelectorAll('.zbtn[data-zoom]');
  for (i = 0; i < zb.length; i++) {{
    zb[i].addEventListener('click', function () {{
      var modo = this.getAttribute('data-zoom');
      if (modo === 'reset') {{
        cur.x = home.x; cur.y = home.y; cur.w = home.w;
        aplicar();
      }} else {{
        acercar(modo === 'in' ? 1 / 1.5 : 1.5,
                cur.x + cur.w / 2, cur.y + (cur.w * ratio) / 2);
      }}
    }});
  }}

  aplicar();
}})();
</script>
</body>
</html>"""


def main() -> int:
    banner("Construyendo tablero")
    with db() as conn:
        meta_all = get_meta(conn)
        run_date = meta_all.get("forecast_run_date")
        if not run_date:
            print("  No hay corrida de pronostico. Ejecuta ingest_forecast.py primero.")
            return 1
        points, thr, hourly, ens = load_state(conn, run_date)
        alerts = load_alerts(conn)
        nino = load_nino(conn)
        verif = load_verification(conn)

    # Fecha de Lima, no la de la maquina: en un runner UTC el dia cambiaria a las 19:00.
    today = datetime.now(ZoneInfo("America/Lima")).date()
    rows = [summarise_point(p, thr.get(p["id"]), hourly, ens.get(p["id"], []), today) for p in points]
    gold_path = DATA_DIR / "gold" / "sisap_dashboard.json"
    sisap_path = gold_path if gold_path.exists() else CACHE_DIR / "sisap_latest.json"
    sisap_records = []
    if sisap_path.exists():
        try:
            sisap_records = json.loads(sisap_path.read_text(encoding="utf-8")).get("records", [])
        except (OSError, json.JSONDecodeError):
            pass
    for row in rows:
        products = set()
        aliases = ALIASES.get(row["id"], ())
        for record in sisap_records:
            origin = norm(record.get("origin_province", ""))
            if origin and any(norm(alias) in origin or origin in norm(alias) for alias in aliases):
                products.add(record["product"])
        row["sisap_products"] = sorted(products)[:8]
    rows.sort(key=lambda r: -r["score"])

    all_dates = sorted({d for r in rows for d in r["daily"]})
    dates = all_dates[:16]

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M") + " (hora local)"
    geodata = load_geodata()
    peru = load_peru()
    roads = load_roads()
    meta = [
        ("Corrida", run_date),
        ("Modelo base", "ECMWF IFS 0.25°"),
        ("Climatología", "ERA5 1991–2020"),
        ("Generado", stamp),
    ]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    html_path = OUT_DIR / "dashboard.html"
    html_path.write_text(
        build_html(rows, dates, meta, stamp, geodata, peru, roads, alerts, nino, verif),
        encoding="utf-8",

    )

    with (OUT_DIR / "pronostico_resumen.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "punto", "departamento", "rol", "modelo", "mm_24h", "mm_72h",
            "max_mm_h", "p95_1d", "p95_3d", "p99_1d", "ratio_vs_umbral",
            "prob25", "score", "nivel",
        ])
        for r in rows:
            w.writerow([
                r["name"], r["dept"], r["rol"], r["model"],
                round(r["next24"], 2), round(r["next72"], 2), round(r["max_hourly"], 2),
                r["p95"], r["p95_3d"], r["p99"], round(r["ratio"], 2),
                round(r["prob25"], 3), r["score"], r["level"],
            ])

    payload = {
        "run_date": run_date,
        "generated": stamp,
        "points": [
            {k: v for k, v in r.items() if k != "daily"} | {"daily": r["daily"]} for r in rows
        ],
    }
    (OUT_DIR / "dashboard_data.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    top = rows[0] if rows else None
    print(f"  Puntos: {len(rows)}  |  fecha de corrida: {run_date}")
    if top:
        print(f"  Mayor riesgo: {top['name']} ({top['level']}, score {top['score']})")
    print(f"  -> {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
