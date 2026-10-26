"""Construye la vista de riesgo de abastecimiento por producto."""

from __future__ import annotations

import html
import json
import math
import unicodedata
from collections import defaultdict

from common import DATA_DIR, OUT_DIR, banner


GOLD = DATA_DIR / "gold" / "sisap_dashboard.json"
FORECAST = OUT_DIR / "dashboard_data.json"
PERU = DATA_DIR / "geodata" / "peru.json"

# Centroides provinciales aproximados: sirven para visualización, no para rutas.
ORIGINS = {
    "arequipa": (-16.40, -71.54), "leoncio prado": (-9.29, -76.00),
    "chanchamayo": (-11.05, -75.33), "satipo": (-11.25, -74.64),
    "trujillo": (-8.11, -79.03), "barranca": (-10.75, -77.76),
    "canete": (-13.08, -76.38), "huaral": (-11.50, -77.21),
    "huarochiri": (-11.92, -76.40), "tambopata": (-12.59, -69.19),
    "oxapampa": (-10.58, -75.40), "morropon": (-5.19, -79.97),
    "sullana": (-4.90, -80.69), "rioja": (-6.06, -77.17),
    "tocache": (-8.19, -76.51), "tumbes": (-3.57, -80.45),
    "atalaya": (-10.73, -73.76), "padre abad": (-9.04, -75.51),
}


def norm(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower().strip()


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371 * 2 * math.asin(min(1, math.sqrt(h)))


def level(score: float) -> str:
    if score >= .55:
        return "Muy alto"
    if score >= .38:
        return "Alto"
    if score >= .20:
        return "Medio"
    return "Bajo"


def project(lon: float, lat: float) -> tuple[float, float]:
    return ((lon + 82) / 14 * 580 + 20, (0 - lat) / 19 * 700 + 10)


def peru_paths() -> str:
    if not PERU.exists():
        return ""
    data = json.loads(PERU.read_text(encoding="utf-8"))
    paths = []
    for ring in data.get("adm0", []):
        if len(ring) < 3:
            continue
        coords = [project(lon, lat) for lon, lat in ring]
        path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in coords) + " Z"
        paths.append(f'<path d="{path}"/>')
    return "".join(paths)


def build_models(records: list[dict], points: list[dict]) -> list[dict]:
    monitored = [(p["name"], (float(p["lat"]), float(p["lon"])), float(p["score"]), p["level"]) for p in points]
    by_product: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        by_product[record["product"]].append(record)

    products = []
    for product, rows in sorted(by_product.items()):
        grouped: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (row.get("origin_department", ""), row.get("origin_province", ""))
            entry = grouped.setdefault(key, {"rows": [], "volume": 0.0})
            entry["rows"].append(row)
            entry["volume"] += float(row.get("volume_t") or 0)
        known = [(key, value) for key, value in grouped.items() if norm(key[1]) in ORIGINS]
        total_volume = sum(value["volume"] for _, value in known)
        use_equal = total_volume <= 0
        origins = []
        for (department, province), info in known:
            coord = ORIGINS[norm(province)]
            nearest = min(monitored, key=lambda p: distance(coord, p[1]))
            share = (1 / len(known)) if use_equal else info["volume"] / total_volume
            avg_prices = [float(r["price_avg"]) for r in info["rows"] if r.get("price_avg") is not None]
            exposure = min(1.0, nearest[2])
            origin_score = .75 * exposure + .25 * share
            origins.append({
                "province": province, "department": department, "lat": coord[0], "lon": coord[1],
                "volume": round(info["volume"], 2), "share": round(share, 4),
                "price": round(sum(avg_prices)/len(avg_prices), 2) if avg_prices else None,
                "climate_score": round(exposure, 3), "climate_level": nearest[3],
                "monitor": nearest[0], "distance_km": round(distance(coord, nearest[1])),
                "score": round(origin_score, 3), "level": level(origin_score),
            })
        origins.sort(key=lambda x: (-x["score"], -x["share"]))
        if origins:
            weighted = sum(x["share"] * x["climate_score"] for x in origins)
            peak = max(x["climate_score"] for x in origins)
            concentration = sum(x["share"] ** 2 for x in origins)
            score = .55 * weighted + .25 * peak + .20 * concentration
        else:
            weighted = peak = concentration = score = 0.0
        products.append({
            "product": product, "score": round(score, 3), "level": level(score),
            "weighted_exposure": round(weighted, 3), "peak_exposure": round(peak, 3),
            "concentration": round(concentration, 3), "origins": origins,
            "mapped_records": sum(len(info["rows"]) for _, info in known), "records": len(rows),
        })
    return sorted(products, key=lambda x: (-x["score"], x["product"]))


def main() -> int:
    banner("Construyendo riesgo de abastecimiento")
    if not GOLD.exists() or not FORECAST.exists():
        print("  Faltan SISAP Gold o dashboard_data.json")
        return 1
    sisap = json.loads(GOLD.read_text(encoding="utf-8"))
    forecast = json.loads(FORECAST.read_text(encoding="utf-8"))
    products = build_models(sisap.get("records", []), forecast.get("points", []))
    payload = json.dumps(products, ensure_ascii=False).replace("</", "<\\/")
    options = "".join(f'<option value="{html.escape(p["product"])}">{html.escape(p["product"])}</option>' for p in products)
    page = f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Riesgo de abastecimiento</title><style>
:root{{--bg:#f5f1e8;--card:#fffdf8;--text:#211d18;--muted:#746a5d;--line:#e7ddca;--yellow:#f4c300}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.wrap{{max-width:1200px;margin:auto;padding:22px 20px 60px}}.nav{{display:flex;gap:8px;overflow:auto;margin-bottom:12px}}.nav a{{white-space:nowrap;padding:8px 14px;border:1px solid #d4b75b;border-radius:999px;text-decoration:none;color:#4b351d;background:#fffdf8;font-weight:650}}.nav a.active{{background:var(--yellow)}}
.hero{{background:linear-gradient(135deg,#15130f,#2a2118 58%,#654621);color:white;border-radius:14px;padding:24px 28px;border-top:5px solid var(--yellow);margin-bottom:16px}}.hero h1{{margin:0 0 5px;font-size:23px}}.hero p{{margin:0;color:#f4e9ca}}.meta{{margin-top:10px;color:#ffe889;font-size:12px}}
.grid{{display:grid;grid-template-columns:minmax(420px,1.25fr) minmax(310px,.75fr);gap:16px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:16px}}h2{{font-size:15px;text-transform:uppercase;margin:0 0 12px}}select{{width:100%;padding:10px;border:1px solid #cdbf9f;border-radius:8px;background:white;font-weight:650;margin-bottom:12px}}
.map{{width:100%;height:auto;max-height:690px;background:#f0eadc;border-radius:10px}}.land path{{fill:#eadfc9;stroke:#9a886b;stroke-width:1}}.marker{{cursor:pointer;stroke:white;stroke-width:2}}.label{{font-size:11px;font-weight:700;paint-order:stroke;stroke:#fff;stroke-width:3px;stroke-linejoin:round}}
.kpis{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}.kpi{{border:1px solid var(--line);border-radius:10px;padding:12px}}.kpi b{{display:block;font-size:23px}}.kpi span{{font-size:10px;color:var(--muted);text-transform:uppercase}}.pill{{display:inline-block;border-radius:999px;padding:4px 9px;font-weight:750;color:white}}.Bajo{{background:#278052}}.Medio{{background:#d19700}}.Alto{{background:#df6b24}}.Muy-alto{{background:#bd2d2d}}
.detail{{margin-top:12px;padding:12px;border-left:4px solid var(--yellow);background:#faf5e8;min-height:105px}}table{{width:100%;border-collapse:collapse;font-size:12px}}th,td{{padding:8px;border-bottom:1px solid var(--line);text-align:left}}th{{font-size:10px;text-transform:uppercase}}td.num{{text-align:right}}.bar{{height:7px;background:#eee4d2;border-radius:9px;overflow:hidden}}.bar i{{display:block;height:100%;background:var(--yellow)}}.note{{font-size:12px;color:#5c5145}}@media(max-width:850px){{.grid{{grid-template-columns:1fr}}.wrap{{padding:12px 10px 40px}}}}
</style></head><body><div class="wrap">
<nav class="nav"><a href="index.html">Clima y riesgo</a><a href="precios.html">Precios SISAP</a><a class="active" href="abastecimiento.html">Riesgo de abastecimiento</a></nav>
<header class="hero"><h1>Riesgo de abastecimiento por producto</h1><p>Procedencias SISAP cruzadas con exposición climática, participación de volumen y concentración geográfica.</p><div class="meta">SISAP {html.escape(sisap.get("date","—"))} · Pronóstico generado {html.escape(forecast.get("generated","—"))}</div></header>
<div class="grid"><section><div class="card"><h2>Mapa de procedencias</h2><select id="product">{options}</select><svg class="map" viewBox="0 0 620 730" role="img" aria-label="Mapa de procedencias"><g class="land">{peru_paths()}</g><g id="markers"></g></svg></div></section>
<aside><div class="card"><h2>Lectura del producto</h2><div id="summary"></div><div class="detail" id="detail">Selecciona un punto del mapa para ver su detalle.</div></div><div class="card"><h2>Cómo se calcula</h2><p class="note"><b>55%</b> exposición climática ponderada por volumen + <b>25%</b> mayor exposición entre procedencias + <b>20%</b> concentración geográfica (HHI). Es una señal de vigilancia, no una predicción de quiebre de stock.</p></div></aside></div>
<div class="card"><h2>Procedencias del producto</h2><div style="overflow:auto"><table><thead><tr><th>Procedencia</th><th>Participación</th><th>Volumen t</th><th>Precio prom.</th><th>Referencia climática</th><th>Riesgo origen</th></tr></thead><tbody id="origins"></tbody></table></div></div>
<div class="card"><h2>Ranking de productos</h2><div style="overflow:auto"><table><thead><tr><th>Producto</th><th>Riesgo</th><th>Exposición ponderada</th><th>Concentración</th><th>Cobertura geográfica</th></tr></thead><tbody id="ranking"></tbody></table></div></div>
</div><script>const DATA={payload};const colors={{"Bajo":"#278052","Medio":"#d19700","Alto":"#df6b24","Muy alto":"#bd2d2d"}};const sel=document.getElementById('product');
function xy(lon,lat){{return [(lon+82)/14*580+20,(0-lat)/19*700+10]}}function pct(x){{return (x*100).toFixed(1)+'%'}}function cssLevel(x){{return x.replace(' ','-')}}
function showDetail(o){{document.getElementById('detail').innerHTML=`<b>${{o.province}}, ${{o.department}}</b><br>Participación: ${{pct(o.share)}} · Volumen: ${{o.volume.toFixed(2)}} t<br>Precio promedio: ${{o.price==null?'—':'S/ '+o.price.toFixed(2)}}<br>Referencia climática: ${{o.monitor}} (${{o.distance_km}} km) · ${{o.climate_level}}`;}}
function render(){{const p=DATA.find(x=>x.product===sel.value)||DATA[0];if(!p)return;sel.value=p.product;document.getElementById('summary').innerHTML=`<div class="kpis"><div class="kpi"><span>Riesgo</span><b>${{(p.score*100).toFixed(1)}}/100</b><i class="pill ${{cssLevel(p.level)}}">${{p.level}}</i></div><div class="kpi"><span>Procedencias mapeadas</span><b>${{p.origins.length}}</b></div><div class="kpi"><span>Exposición ponderada</span><b>${{pct(p.weighted_exposure)}}</b></div><div class="kpi"><span>Concentración HHI</span><b>${{p.concentration.toFixed(2)}}</b></div></div><p class="note">Cobertura: ${{p.mapped_records}} de ${{p.records}} registros con provincia georreferenciada.</p>`;
const g=document.getElementById('markers');g.innerHTML='';p.origins.forEach(o=>{{const [x,y]=xy(o.lon,o.lat);const c=document.createElementNS('http://www.w3.org/2000/svg','circle');c.setAttribute('cx',x);c.setAttribute('cy',y);c.setAttribute('r',7+Math.sqrt(o.share)*15);c.setAttribute('fill',colors[o.level]);c.setAttribute('class','marker');c.setAttribute('tabindex','0');c.onclick=()=>showDetail(o);g.appendChild(c);const t=document.createElementNS('http://www.w3.org/2000/svg','text');t.setAttribute('x',x+11);t.setAttribute('y',y+4);t.setAttribute('class','label');t.textContent=o.province;g.appendChild(t);}});
document.getElementById('origins').innerHTML=p.origins.map(o=>`<tr><td><b>${{o.province}}</b><br><small>${{o.department}}</small></td><td>${{pct(o.share)}}<div class="bar"><i style="width:${{pct(o.share)}}"></i></div></td><td class="num">${{o.volume.toFixed(2)}}</td><td class="num">${{o.price==null?'—':'S/ '+o.price.toFixed(2)}}</td><td>${{o.monitor}} · ${{o.climate_level}}</td><td><span class="pill ${{cssLevel(o.level)}}">${{o.level}}</span></td></tr>`).join('');if(p.origins[0])showDetail(p.origins[0]);}}
document.getElementById('ranking').innerHTML=DATA.map(p=>`<tr><td><b>${{p.product}}</b></td><td><span class="pill ${{cssLevel(p.level)}}">${{(p.score*100).toFixed(1)}} · ${{p.level}}</span></td><td>${{pct(p.weighted_exposure)}}</td><td>${{p.concentration.toFixed(2)}}</td><td>${{p.mapped_records}}/${{p.records}}</td></tr>`).join('');sel.onchange=render;render();</script></body></html>'''
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / "abastecimiento.html"
    target.write_text(page, encoding="utf-8")
    print(f"  -> {target} ({len(products)} productos)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
