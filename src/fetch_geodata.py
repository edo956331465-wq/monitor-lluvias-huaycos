"""Descarga la geometria real que usa el tablero.

Dos fuentes, ambas abiertas:

  - OpenStreetMap (API Overpass) para rios y quebradas de las zonas priorizadas.
  - geoBoundaries para el contorno del Peru y sus 25 regiones.

Los resultados se simplifican (Ramer-Douglas-Peucker) y se guardan en
data/geodata/. El tablero dibuja a partir de esos archivos, asi que no inventa
trazados: lo que se ve es lo que hay mapeado en la fuente.

Uso:
    python fetch_geodata.py            # usa cache si existe
    python fetch_geodata.py --refresh  # vuelve a consultar las fuentes
"""

from __future__ import annotations

import json
import math
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from common import DATA_DIR, banner

GEODATA_DIR = DATA_DIR / "geodata"
GEODATA_PATH = GEODATA_DIR / "waterways.json"
PERU_PATH = GEODATA_DIR / "peru.json"
ROADS_PATH = GEODATA_DIR / "roads.json"

BOUNDARY_API = "https://www.geoboundaries.org/api/current/gbOpen/PER/{level}/"
BOUNDARY_SIMPLIFY = 0.02  # grados (~2 km): suficiente para un mapa nacional

# Red vial nacional publicada por SUTRAN en su CDN (mismo origen que el mapa
# interactivo de alertas). Si cambia la URL, hay que volver a leer su bundle.
ROADS_URL = "https://d27ijeauab3idp.cloudfront.net/red-vial-nacional.geojson"
ROADS_DEP_URL = "https://d27ijeauab3idp.cloudfront.net/red-vial-departamental.geojson"
ROADS_SIMPLIFY = 0.0035     # ~400 m: suficiente para mirar de cerca
ROADS_DEP_SIMPLIFY = 0.005  # ~550 m en la red departamental

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# bbox = (sur, oeste, norte, este)
ZONES = {
    "chosica": {
        "label": "Chosica y Chaclacayo · Lima Este",
        "cuenca": "Cuenca baja del Rímac",
        "bbox": (-12.03, -76.86, -11.85, -76.55),
        "points": ["chosica", "santa-eulalia"],
        "exclude": ["Río Lurín"],  # cuenca vecina: estira el encuadre sin aportar
        "nota": "Quebradas con diques y población en faja marginal según Defensoría del Pueblo (2026).",
    },
    "piura": {
        "label": "Piura y Sullana",
        "cuenca": "Cuencas del Piura y el Chira",
        "bbox": (-5.45, -80.85, -4.80, -80.40),
        "points": ["piura", "sullana"],
        "exclude": [],
        "nota": "El riesgo dominante es fluvial (río Piura y Chira) más que de quebrada.",
    },
    "tumbes": {
        "label": "Tumbes y Zarumilla",
        "cuenca": "Cuencas del Tumbes y el Zarumilla",
        "bbox": (-3.80, -80.65, -3.40, -80.20),
        "points": ["tumbes"],
        "exclude": [],
        "nota": "Cuencas binacionales; el desborde de río es el mecanismo principal.",
    },
}

KEEP_KINDS = {"river", "stream"}
DROP_PREFIXES = ("dren", "canal", "sifón", "sifon", "drenaje", "acequia")
# Nombres que no son cauces: calles, pasajes y etiquetas genericas de OSM.
BAD_PREFIXES = ("pasaje", "calle", "avenida", "jirón", "jiron", "jr.")
BAD_EXACT = {"quebrada", "qda", "rio", "río", "canal"}
RDP_EPSILON = 0.00025  # grados (~28 m)


def overpass(query: str) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    last: Exception | None = None
    for url in MIRRORS:
        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    url, data=data, headers={"User-Agent": "MonitorLluviasPeru/0.1"}
                )
                with urllib.request.urlopen(req, timeout=150) as resp:
                    return json.loads(resp.read())
            except Exception as exc:  # noqa: BLE001
                last = exc
                continue
    raise RuntimeError(f"Overpass no respondio: {last}")


def keep_feature(tags: dict, exclude: list[str]) -> bool:
    name = (tags.get("name") or "").strip()
    kind = tags.get("waterway") or ""
    if not name:
        return False
    low = name.lower()
    if low in BAD_EXACT or low.startswith(BAD_PREFIXES):
        return False
    if low.startswith(DROP_PREFIXES):
        return False
    if name in exclude:
        return False
    if "quebrada" in low or low.startswith("qda"):
        return True
    return kind in KEEP_KINDS


def _perpendicular(p, a, b) -> float:
    if a == b:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    dx, dy = b[0] - a[0], b[1] - a[1]
    num = abs(dy * p[0] - dx * p[1] + b[0] * a[1] - b[1] * a[0])
    return num / math.hypot(dx, dy)


def simplify(points: list[list[float]], eps: float = RDP_EPSILON) -> list[list[float]]:
    """Ramer-Douglas-Peucker iterativo."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        worst, index = 0.0, -1
        for i in range(start + 1, end):
            d = _perpendicular(points[i], points[start], points[end])
            if d > worst:
                worst, index = d, i
        if index != -1 and worst > eps:
            keep[index] = True
            stack.append((start, index))
            stack.append((index, end))
    return [p for p, k in zip(points, keep) if k]


def fetch_zone(zone_id: str, zone: dict) -> list[dict]:
    south, west, north, east = zone["bbox"]
    query = (
        f'[out:json][timeout:120];'
        f'way["waterway"]["name"]({south},{west},{north},{east});'
        f"out geom;"
    )
    payload = overpass(query)
    features = []
    for el in payload.get("elements", []):
        tags = el.get("tags", {})
        if not keep_feature(tags, zone.get("exclude", [])):
            continue
        geom = el.get("geometry") or []
        coords = [[round(pt["lon"], 4), round(pt["lat"], 4)] for pt in geom if "lon" in pt]
        if len(coords) < 2:
            continue
        simplified = simplify(coords)
        features.append(
            {
                # "Qubebrada" es una errata real en OSM; se corrige solo para mostrar.
                "name": tags["name"].strip().replace("Qubebrada", "Quebrada"),
                "kind": tags.get("waterway", "stream"),
                "coords": simplified,
            }
        )
    return features


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "MonitorLluviasPeru/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def _outer_rings(geometry: dict) -> list[list[list[float]]]:
    kind = geometry.get("type")
    if kind == "Polygon":
        return [geometry["coordinates"][0]]
    if kind == "MultiPolygon":
        return [poly[0] for poly in geometry["coordinates"]]
    return []


def _simplify_ring(ring: list, eps: float = BOUNDARY_SIMPLIFY) -> list[list[float]]:
    pts = [[round(c[0], 3), round(c[1], 3)] for c in ring]
    if len(pts) < 4:
        return pts
    closed = pts[0] == pts[-1]
    core = pts[:-1] if closed else pts
    simple = simplify(core, eps)
    return simple + [simple[0]] if closed else simple


def fetch_peru() -> dict:
    """Contorno nacional y regiones desde geoBoundaries (gbOpen)."""
    meta0 = _get_json(BOUNDARY_API.format(level="ADM0"))
    item0 = meta0 if isinstance(meta0, dict) else meta0[0]
    gj0 = _get_json(item0["simplifiedGeometryGeoJSON"])

    meta1 = _get_json(BOUNDARY_API.format(level="ADM1"))
    item1 = meta1 if isinstance(meta1, dict) else meta1[0]
    gj1 = _get_json(item1["simplifiedGeometryGeoJSON"])

    adm0 = []
    for feature in gj0.get("features", []):
        for ring in _outer_rings(feature.get("geometry") or {}):
            adm0.append(_simplify_ring(ring))

    regions = []
    for feature in gj1.get("features", []):
        props = feature.get("properties") or {}
        name = props.get("shapeName") or props.get("name") or "?"
        rings = [_simplify_ring(r) for r in _outer_rings(feature.get("geometry") or {})]
        if rings:
            regions.append({"name": name, "rings": rings})

    return {
        "source": "geoBoundaries gbOpen (límites administrativos abiertos)",
        "adm0": adm0,
        "regions": regions,
    }


def _lines_from_geojson(gj: dict, eps: float) -> list[list[list[float]]]:
    lines: list[list[list[float]]] = []
    for feature in gj.get("features", []):
        geometry = feature.get("geometry") or {}
        kind = geometry.get("type")
        if kind == "LineString":
            raw = [geometry["coordinates"]]
        elif kind == "MultiLineString":
            raw = geometry["coordinates"]
        else:
            continue
        for part in raw:
            pts = [[round(c[0], 3), round(c[1], 3)] for c in part]
            if len(pts) < 2:
                continue
            simple = simplify(pts, eps)
            if len(simple) >= 2:
                lines.append(simple)
    return lines


def fetch_roads() -> dict:
    """Red vial nacional y departamental (SUTRAN), en dos niveles de detalle.

    La nacional se dibuja siempre; la departamental solo cuando el mapa se
    acerca, para no convertir el pais en una maraña a escala 1x.
    """
    nacional = _lines_from_geojson(_get_json(ROADS_URL), ROADS_SIMPLIFY)
    departamental = _lines_from_geojson(_get_json(ROADS_DEP_URL), ROADS_DEP_SIMPLIFY)

    return {
        "source": "SUTRAN · red vial nacional y departamental",
        "nacional": nacional,
        "departamental": departamental,
    }


def main() -> int:
    refresh = "--refresh" in sys.argv
    GEODATA_DIR.mkdir(parents=True, exist_ok=True)

    if ROADS_PATH.exists() and not refresh:
        print(f"  Red vial en cache: {ROADS_PATH.name}")
    else:
        try:
            roads = fetch_roads()
            ROADS_PATH.write_text(
                json.dumps(roads, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
            )
            n_pts = sum(len(line) for line in roads["nacional"])
            d_pts = sum(len(line) for line in roads["departamental"])
            print(
                f"  vial       nacional {len(roads['nacional'])} tramos/{n_pts:,} pts · "
                f"departamental {len(roads['departamental'])} tramos/{d_pts:,} pts · "
                f"{ROADS_PATH.stat().st_size / 1024:.0f} KB"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  vial       ERROR: {exc}")

    if PERU_PATH.exists() and not refresh:
        print(f"  Contorno del Peru en cache: {PERU_PATH.name}")
    else:
        try:
            peru = fetch_peru()
            PERU_PATH.write_text(
                json.dumps(peru, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
            )
            print(
                f"  peru       {len(peru['regions'])} regiones, "
                f"{PERU_PATH.stat().st_size / 1024:.0f} KB"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  peru       ERROR: {exc}")

    if GEODATA_PATH.exists() and not refresh:
        cached = json.loads(GEODATA_PATH.read_text(encoding="utf-8"))
        print(f"  Cache vigente: {GEODATA_PATH} ({len(cached.get('zones', {}))} zonas)")
        return 0

    banner("Descargando geometria de rios y quebradas (OSM / Overpass)")
    out = {"fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "zones": {}}

    for zone_id, zone in ZONES.items():
        try:
            features = fetch_zone(zone_id, zone)
            names = sorted({f["name"] for f in features})
            out["zones"][zone_id] = {
                "label": zone["label"],
                "cuenca": zone["cuenca"],
                "bbox": list(zone["bbox"]),
                "points": zone["points"],
                "nota": zone["nota"],
                "features": features,
            }
            print(f"  {zone_id:<10} {len(features):>3} tramos, {len(names)} cursos: {', '.join(names[:5])}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {zone_id:<10} ERROR: {exc}")

    if not out["zones"]:
        print("  No se obtuvo nada. Se conserva el archivo anterior si existia.")
        return 1

    GEODATA_PATH.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    size_kb = GEODATA_PATH.stat().st_size / 1024
    print(f"\n  -> {GEODATA_PATH} ({size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
