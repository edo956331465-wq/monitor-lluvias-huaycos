"""Publica una vista Gold ligera de SISAP para GitHub Pages.

Este proceso se ejecuta localmente porque SISAP no responde de forma fiable a
los runners de GitHub. Lee la capa Silver del proyecto ``Precios Mayoristas``
y escribe únicamente el contrato de datos que consume el dashboard.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


HERE = Path(__file__).resolve().parents[1]
TARGET = HERE / "data" / "gold" / "sisap_dashboard.json"
PURCHASE_LIST = HERE / "input" / "Lista de Productos.xlsx"

# Catálogo gobernado: la clave es el producto oficial SISAP y los valores son
# expresiones observadas en el maestro interno. El orden resuelve casos específicos
# antes que los genéricos (p. ej. ají seco antes que ají fresco).
PRODUCT_RULES = {
    "aji seco": ("aji panca seco", "aji mirasol"),
    "aji fresco": ("aji amarillo", "aji limo", "aji charapita", "rocoto"),
    "arveja grano verde": ("arveja", "alverja"),
    "frijol grano seco": ("frejol canario", "frejol panamito", "frejol castilla"),
    "frijol grano verde": ("frejol verde",),
    "haba grano verde": ("haba", "habas"),
    "lenteja grano seco": ("lenteja",),
    "hortalizas chinas": ("frejol chino", "moyashi", "kion", "pac choy", "choy sam", "col china", "hakusai", "brocoli", "holantao"),
    "bayas": ("frambuesa", "mora", "zarzamora"),
    "otros fruticolas": ("arandano", "arandanos", "aguaymanto", "carambola", "tumbo", "camu camu", "lima"),
    "maiz seco": ("maiz morado",),
    "melocoton": ("durazno",),
    "pina": ("pina",),
    "platano": ("platano",),
    "pimiento fresco": ("pimiento",),
    "esparrago": ("esparrago", "esparragos"),
    "vainita": ("vainita",),
    "alcachofa": ("alcachofa",),
    "ajo": ("ajo",), "apio": ("apio",), "beterraga": ("beterraga",),
    "camote": ("camote",), "cebolla": ("cebolla",), "chirimoya": ("chirimoya",),
    "choclo": ("choclo",), "culantro": ("culantro",), "espinaca": ("espinaca",),
    "fresa": ("fresa",), "granadilla": ("granadilla",), "lechuga": ("lechuga",),
    "limon": ("limon",), "mandarina": ("mandarina",), "mango": ("mango",),
    "manzana": ("manzana",), "maracuya": ("maracuya",), "melon": ("melon",),
    "membrillo": ("membrillo",), "naranja": ("naranja",), "olluco": ("olluco",),
    "palta": ("palta",), "papa": ("papa",), "papaya": ("papaya",),
    "pepino": ("pepino",), "sandia": ("sandia",), "tomate": ("tomate",),
    "tuna": ("tuna",), "uva": ("uva",), "yuca": ("yuca",),
    "zanahoria": ("zanahoria",), "zapallo": ("zapallo",),
}

DEPARTMENTS = {
    "AMAZONAS": "Amazonas", "ANCASH": "Áncash", "APURIMAC": "Apurímac",
    "AREQUIPA": "Arequipa", "AYACUCHO": "Ayacucho", "CAJAMARCA": "Cajamarca",
    "CALLAO": "Callao", "CUSCO": "Cusco", "HUANCAVELICA": "Huancavelica",
    "HUANUCO": "Huánuco", "ICA": "Ica", "JUNIN": "Junín", "LA LIBERTAD": "La Libertad",
    "LAMBAYEQUE": "Lambayeque", "LIMA": "Lima", "LORETO": "Loreto",
    "MADRE DE DIOS": "Madre de Dios", "MOQUEGUA": "Moquegua", "PASCO": "Pasco",
    "PIURA": "Piura", "PUNO": "Puno", "SAN MARTIN": "San Martín", "TACNA": "Tacna",
    "TUMBES": "Tumbes", "UCAYALI": "Ucayali",
}

PROVINCE_ACCENTS = {
    "ASUNCION": "Asunción", "BOLOGNESI": "Bolognesi", "CANETE": "Cañete",
    "CHACHAPOYAS": "Chachapoyas", "CONCEPCION": "Concepción", "HUANUCO": "Huánuco",
    "HUAROCHIRI": "Huarochirí", "HUAYTARA": "Huaytará", "JAEN": "Jaén",
    "LA CONVENCION": "La Convención", "LEONCIO PRADO": "Leoncio Prado",
    "MARANON": "Marañón", "MORROPON": "Morropón", "NAZCA": "Nasca",
    "OYON": "Oyón", "PARINACOCHAS": "Parinacochas", "SANCHEZ CARRION": "Sánchez Carrión",
    "SAN MARTIN": "San Martín", "VILCAS HUAMAN": "Vilcas Huamán",
}

PRODUCT_LABELS = {
    "aji fresco": "Ají fresco", "aji seco": "Ají seco", "esparrago": "Espárrago",
    "frijol grano seco": "Frijol grano seco", "frijol grano verde": "Frijol grano verde",
    "limon": "Limón", "maiz seco": "Maíz seco", "pina": "Piña",
}


def norm(value: object) -> str:
    text = "" if pd.isna(value) else str(value)
    # Corrige mojibake frecuente en los CSV históricos antes de quitar tildes.
    try:
        if "\ufffd" in text or "Ã" in text or "�" in text:
            text = text.encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
    except UnicodeError:
        pass
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def find_project(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    for parent in HERE.parents:
        candidate = parent / "Precios Mayoristas"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("No se encontró el proyecto hermano 'Precios Mayoristas'.")


def value(row: pd.Series, column: str) -> float | None:
    item = row.get(column)
    return None if pd.isna(item) else float(item)


def build_purchase_catalog() -> tuple[list[dict], set[str]]:
    if not PURCHASE_LIST.exists():
        raise FileNotFoundError(f"No existe la lista de compra: {PURCHASE_LIST}")
    source = pd.read_excel(PURCHASE_LIST, dtype={"Item": "string", "PRODUCTO": "string"})
    catalog = []
    for _, row in source.iterrows():
        original = "" if pd.isna(row.get("PRODUCTO")) else str(row["PRODUCTO"]).strip()
        normalized = norm(original)
        matches = [(key, alias) for key, aliases in PRODUCT_RULES.items() for alias in aliases
                   if re.search(rf"\b{re.escape(alias)}\b", normalized)]
        match = max(matches, key=lambda pair: len(pair[1])) if matches else None
        catalog.append({
            "item_code": "" if pd.isna(row.get("Item")) else str(row["Item"]).removesuffix(".0"),
            "internal_name": original, "internal_name_norm": normalized,
            "sisap_product_key": match[0] if match else None,
            "match_rule": match[1] if match else None,
            "match_status": "matched_curated_rule" if match else "not_available_in_sisap_catalog",
        })
    return catalog, {row["sisap_product_key"] for row in catalog if row["sisap_product_key"]}


def canonical_origin(row: pd.Series) -> dict:
    raw_province = "" if pd.isna(row.get("procedencia")) else str(row["procedencia"]).strip()
    raw_department = "" if pd.isna(row.get("departamento_procedencia")) else str(row["departamento_procedencia"]).strip()
    foreign = norm(raw_province) in {"argentina", "chile", "ecuador", "usa"}
    unknown = not raw_province or norm(raw_province) == "procedencia desconocida"
    department = DEPARTMENTS.get(norm(raw_department).upper(), raw_department.title() if raw_department else "")
    province_key = norm(raw_province).upper()
    province = PROVINCE_ACCENTS.get(province_key, raw_province.title()) if raw_province else ""
    country = ({"usa": "Estados Unidos"}.get(norm(raw_province), raw_province.title()) if foreign else "Perú")
    if unknown:
        country, province, department = "", "Procedencia desconocida", ""
    origin_type = "unknown" if unknown else "international" if foreign else "domestic"
    match = "" if pd.isna(row.get("ubigeo_match")) else str(row["ubigeo_match"]).strip().upper()
    quality = "official_ubigeo" if match == "SI" else "country_normalized" if foreign else "unresolved" if unknown else "source_text"
    key = f"PE-{row.get('ubigeo_provincia')}" if match == "SI" else f"INT-{norm(country).upper().replace(' ', '-')}" if foreign else "UNKNOWN"
    return {
        "origin_raw": raw_province, "origin_department_raw": raw_department,
        "origin_country": country, "origin_department": department, "origin_province": province,
        "origin_type": origin_type, "origin_key": key, "origin_quality": quality,
        "origin_department_code": "" if pd.isna(row.get("ubigeo_departamento")) else str(row["ubigeo_departamento"]),
        "origin_province_code": "" if pd.isna(row.get("ubigeo_provincia")) else str(row["ubigeo_provincia"]),
    }


def product_label(key: str) -> str:
    return PRODUCT_LABELS.get(key, key.capitalize())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--precios-root", help="Ruta del proyecto Precios Mayoristas")
    args = parser.parse_args()
    project = find_project(args.precios_root)
    silver = project / "Arquitectura Medallón" / "Output" / "silver" / "sisap_mercados"
    files = sorted(silver.glob("anio=*/data.parquet"))
    if not files:
        raise FileNotFoundError(f"No existen particiones Silver en {silver}")

    # Se leen las dos particiones más recientes para cubrir una ventana móvil
    # de 365 días incluso cuando cruza el cambio de año.
    frame = pd.concat([pd.read_parquet(path) for path in files[-2:]], ignore_index=True)
    frame["fecha"] = pd.to_datetime(frame["fecha"], errors="coerce")
    frame = frame[frame["fecha"].notna()].copy()
    purchase_catalog, products = build_purchase_catalog()
    frame["producto_filtro"] = frame["producto_norm"].map(norm)
    frame = frame[frame["producto_filtro"].isin(products)].copy()

    selected = []
    market_dates: dict[str, str] = {}
    for market, group in frame.groupby("mercado", dropna=False):
        latest = group["fecha"].max()
        group = group[group["fecha"] == latest]
        market_name = str(market)
        market_dates[market_name] = latest.strftime("%d/%m/%Y")
        selected.append(group)
    latest_rows = pd.concat(selected, ignore_index=True) if selected else frame.iloc[0:0]

    records = []
    for _, row in latest_rows.iterrows():
        records.append({
            "market_code": "" if pd.isna(row.get("mercado_codigo")) else str(row["mercado_codigo"]),
            "market": str(row["mercado"]),
            "product": product_label(str(row["producto_filtro"])),
            "variety": "" if pd.isna(row.get("variedad")) else str(row["variedad"]),
            "price_min": value(row, "precio_min"),
            "price_avg": value(row, "precio_promedio"),
            "price_max": value(row, "precio_max"),
            "volume_t": value(row, "volumen_t"),
            **canonical_origin(row),
            "date": row["fecha"].strftime("%d/%m/%Y"),
        })

    # Red histórica producto-procedencia. Evita perder un origen conocido solo
    # porque no registró ingreso durante la fecha más reciente.
    latest_date = frame["fecha"].max()
    history_start = latest_date - timedelta(days=364)
    history = frame[frame["fecha"] >= history_start].copy()
    history = history[
        history["procedencia"].notna()
        & history["procedencia"].astype("string").str.strip().ne("")
    ].copy()
    history["volumen_t"] = pd.to_numeric(history["volumen_t"], errors="coerce")
    history["precio_promedio"] = pd.to_numeric(history["precio_promedio"], errors="coerce")
    origin_history = []
    keys = ["producto_filtro", "departamento_procedencia", "procedencia"]
    for (product_key, department, province), group in history.groupby(keys, dropna=False):
        label = product_label(str(product_key))
        volume = group["volumen_t"].sum(min_count=1)
        price = group["precio_promedio"].mean()
        origin_history.append({
            "product_key": str(product_key),
            "product": label,
            **canonical_origin(group.iloc[-1]),
            "first_seen": group["fecha"].min().strftime("%d/%m/%Y"),
            "last_seen": group["fecha"].max().strftime("%d/%m/%Y"),
            "active_days": int(group["fecha"].dt.date.nunique()),
            "record_count": int(len(group)),
            "market_count": int(group["mercado"].nunique()),
            "volume_t": None if pd.isna(volume) else round(float(volume), 2),
            "price_avg": None if pd.isna(price) else round(float(price), 2),
        })

    now = datetime.now(ZoneInfo("America/Lima"))
    payload = {
        "contract_version": 3,
        "layer": "gold",
        "source": "SISAP · MIDAGRI vía Arquitectura Medallón",
        "source_url": "http://sistemas.midagri.gob.pe/sisap/portal2/mayorista/",
        "date": max(market_dates.values(), key=lambda x: datetime.strptime(x, "%d/%m/%Y"), default="—"),
        "market_dates": market_dates,
        "fetched_at": now.isoformat(timespec="minutes"),
        "records": records,
        "origin_history": origin_history,
        "purchase_selection": [
            {"product_key": key, "product": product_label(key),
             "internal_item_count": sum(row["sisap_product_key"] == key for row in purchase_catalog)}
            for key in sorted(products)
        ],
        "history_window": {
            "days": 365,
            "from": history_start.strftime("%d/%m/%Y"),
            "to": latest_date.strftime("%d/%m/%Y"),
        },
        "errors": [],
        "quality": {
            "input_items": len(purchase_catalog),
            "matched_input_items": sum(row["match_status"] == "matched_curated_rule" for row in purchase_catalog),
            "sisap_products_requested": len(products),
            "daily_records": len(records),
            "history_relations": len(origin_history),
            "origins_official_ubigeo": sum(row["origin_quality"] == "official_ubigeo" for row in origin_history),
            "origins_unresolved": sum(row["origin_quality"] == "unresolved" for row in origin_history),
        },
        "lineage": {
            "bronze": "Precios Mayoristas/Arquitectura Medallón/Output/bronze/sisap_mercados",
            "silver": f"Precios Mayoristas/Arquitectura Medallón/Output/silver/sisap_mercados/{files[-1].parent.name}/data.parquet",
            "gold": "data/gold/sisap_dashboard.json",
        },
    }
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    temporary = TARGET.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(TARGET)
    print(
        f"OK {TARGET}: {len(records)} filas diarias, "
        f"{len(origin_history)} relaciones históricas; fecha máxima {payload['date']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
