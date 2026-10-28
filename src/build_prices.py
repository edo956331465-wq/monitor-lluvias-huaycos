"""Construye la segunda hoja del sitio: precios y procedencias SISAP."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections import defaultdict

from common import CACHE_DIR, DATA_DIR, OUT_DIR, banner, load_points

GOLD_SOURCE = DATA_DIR / "gold" / "sisap_dashboard.json"
CACHE_SOURCE = CACHE_DIR / "sisap_latest.json"


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


ALIASES = {
    "tumbes": ("tumbes", "zarumilla", "contralmirante villar"),
    "piura": ("piura", "sechura", "morropon"),
    "sullana": ("sullana", "paita", "talara", "ayabaca"),
    "huancabamba": ("huancabamba",),
    "chiclayo": ("chiclayo", "lambayeque", "ferrenafe"),
    "trujillo": ("trujillo", "viru", "asche", "otuzco"),
    "chimbote": ("santa", "chimbote"),
    "casma": ("casma", "huarmey"),
    "barranca": ("barranca", "cajatambo"),
    "huacho": ("huaura", "huacho", "oyon"),
    "lima": ("lima", "callao", "huaral"),
    "chosica": ("huarochiri", "chosica"),
    "santa-eulalia": ("huarochiri", "santa eulalia"),
    "canete": ("canete", "mala"),
    "yauyos": ("yauyos",),
    "pisco": ("pisco", "chincha"),
    "ica": ("ica", "palpa", "nazca"),
    "camana": ("camana", "caraveli"),
    "mollendo": ("islay", "mollendo", "arequipa"),
    "ilo": ("ilo", "mariscal nieto", "moquegua"),
    "tacna": ("tacna", "jorge basadre"),
}


CSS = """
:root{--bg:#f5f1e8;--card:#fffdf8;--text:#211d18;--muted:#746a5d;--line:#e7ddca;
--accent:#5a3b22;--yellow:#f4c300}*{box-sizing:border-box}body{margin:0;background:var(--bg);
color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:auto;padding:22px 20px 60px}.nav{display:flex;gap:8px;margin-bottom:12px}
.nav a{padding:8px 14px;border:1px solid #d4b75b;border-radius:999px;text-decoration:none;
color:#4b351d;background:#fffdf8;font-weight:650}.nav a.active{background:var(--yellow);border-color:var(--yellow)}
.hero{background:linear-gradient(135deg,#15130f,#2a2118 58%,#654621);color:white;border-radius:14px;
padding:25px 28px;border-top:5px solid var(--yellow);margin-bottom:16px}.hero h1{margin:0 0 5px;font-size:23px}
.hero p{margin:0;color:#f4e9ca}.meta{margin-top:12px;color:#ffe889;font-size:12px}.kpis{display:grid;
grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:16px}.kpi,.card{background:var(--card);
border:1px solid var(--line);border-radius:12px}.kpi{padding:14px 16px}.kpi b{display:block;font-size:25px}
.kpi span{font-size:11px;color:var(--muted);text-transform:uppercase}.card{padding:18px 20px;margin-bottom:16px}
h2{font-size:15px;text-transform:uppercase;color:#3b2a1c;margin:0 0 10px}.origins{display:grid;
grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}.origin{border:1px solid var(--line);border-radius:10px;
padding:12px;background:#fff}.origin h3{font-size:13px;margin:0 0 6px}.chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{background:#fff0a8;border:1px solid #e5c44c;border-radius:999px;padding:2px 8px;font-size:11px}
.tablewrap{overflow:auto;max-height:700px}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:8px;
border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{position:sticky;top:0;background:#faf5e8;
font-size:10px;text-transform:uppercase}td.num{text-align:right;font-variant-numeric:tabular-nums}tbody tr:hover{background:#fbf6e9}
.note{padding:10px 12px;background:#faf5e8;border-left:3px solid var(--yellow);color:#51483e;font-size:12px}
footer{text-align:center;color:var(--muted);font-size:11px;margin-top:22px}@media(max-width:650px){.wrap{padding:12px 10px 40px}.hero{padding:20px}.nav{overflow:auto}}
"""


def fmt(value, decimals=2):
    return "—" if value is None else f"{value:,.{decimals}f}".replace(",", " ")


def main() -> int:
    banner("Construyendo hoja de precios SISAP")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source = GOLD_SOURCE if GOLD_SOURCE.exists() else CACHE_SOURCE
    if source.exists():
        payload = json.loads(source.read_text(encoding="utf-8"))
    else:
        payload = {"date": "—", "fetched_at": "—", "records": [], "errors": ["Sin datos SISAP"]}
    records = payload.get("records", [])
    points = {p["id"]: p for p in load_points()}

    nearby: dict[str, set[str]] = defaultdict(set)
    for record in records:
        origin = norm(record.get("origin_province", ""))
        for point_id, aliases in ALIASES.items():
            if any(norm(alias) in origin or origin in norm(alias) for alias in aliases if origin):
                nearby[point_id].add(record["product"])

    origin_cards = []
    for point_id, products in sorted(nearby.items(), key=lambda item: points[item[0]]["lat"], reverse=True):
        chips = "".join(f'<span class="chip">{html.escape(p)}</span>' for p in sorted(products))
        point = points[point_id]
        origin_cards.append(
            f'<div class="origin"><h3>{html.escape(point["name"])} · {html.escape(point["dept"])}</h3>'
            f'<div class="chips">{chips}</div></div>'
        )

    table_rows = []
    for r in sorted(records, key=lambda x: (x["product"], x["variety"], x["market"], x["origin_province"])):
        table_rows.append(
            "<tr>" +
            f'<td>{html.escape(r["product"])}</td><td>{html.escape(r["variety"])}</td>' +
            f'<td>{html.escape(r.get("date", "—"))}</td><td>{html.escape(r["market"])}</td>' +
            f'<td>{html.escape(r["origin_province"])}</td>' +
            f'<td class="num">{fmt(r["price_min"])}</td><td class="num"><b>{fmt(r["price_avg"])}</b></td>' +
            f'<td class="num">{fmt(r["price_max"])}</td><td class="num">{fmt(r["volume_t"], 1)}</td></tr>'
        )

    unique_products = len({r["product"] for r in records})
    unique_origins = len({r["origin_province"] for r in records if r["origin_province"] != "Procedencia desconocida"})
    unique_markets = len({r["market"] for r in records})
    errors = payload.get("errors", [])
    warning = (f'<div class="note">Advertencias de la última descarga: {html.escape(" · ".join(errors))}</div>'
               if errors else '<div class="note">Precios referenciales reportados por SISAP; la unidad depende del producto.</div>')

    page = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Precios y procedencias · SISAP</title>
<style>{CSS}</style></head><body><div class="wrap">
<nav class="nav"><a href="index.html">Clima y riesgo</a><a class="active" href="precios.html">Precios SISAP</a><a href="abastecimiento.html">Riesgo de abastecimiento</a><a href="puertos.html">Puertos y mar</a><a href="pescados.html">Pescados y mariscos</a></nav>
<header class="hero"><h1>Precios y procedencias · SISAP</h1><p>Productos relevantes para la operación, mercados mayoristas de Lima y zonas de origen próximas a la red climática.</p>
<div class="meta">Fecha SISAP: {html.escape(payload.get("date", "—"))} · Descarga: {html.escape(payload.get("fetched_at", "—"))}</div></header>
<div class="kpis"><div class="kpi"><span>Productos</span><b>{unique_products}</b></div>
<div class="kpi"><span>Mercados con datos</span><b>{unique_markets}</b></div>
<div class="kpi"><span>Procedencias</span><b>{unique_origins}</b></div>
<div class="kpi"><span>Registros</span><b>{len(records)}</b></div></div>
<div class="card"><h2>Productos cerca de puntos monitoreados</h2>{warning}<div style="height:10px"></div>
<div class="origins">{"".join(origin_cards) or '<p>No se encontraron coincidencias de procedencia.</p>'}</div></div>
<div class="card"><h2>Detalle de precios mayoristas</h2><div class="tablewrap"><table><thead><tr>
<th>Producto</th><th>Variedad</th><th>Fecha</th><th>Mercado</th><th>Procedencia</th><th>Mín.</th><th>Prom.</th><th>Máx.</th><th>Volumen t</th>
</tr></thead><tbody>{"".join(table_rows)}</tbody></table></div></div>
<footer>Fuente: <a href="http://sistemas.midagri.gob.pe/sisap/portal2/mayorista/">SISAP · MIDAGRI</a>. Procedencia aproximada por provincia; no representa el predio exacto de producción.</footer>
</div></body></html>"""
    target = OUT_DIR / "precios.html"
    target.write_text(page, encoding="utf-8")
    print(f"  -> {target} ({len(records)} filas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
