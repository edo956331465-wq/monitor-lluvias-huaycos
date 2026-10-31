"""Construye el contrato Gold combinado de precios SISAP y EMMSA.

La lista interna se usa localmente para gobernar la selección. El repositorio
público recibe solo categorías agregadas y un manifiesto de linaje.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from export_sisap_medallion import (
    HERE, TARGET as SISAP_GOLD, build_purchase_catalog, find_project, norm, product_label,
)

BRONZE = HERE / "data" / "bronze" / "market_prices" / "manifest.json"
SILVER = HERE / "data" / "silver" / "market_prices_catalog.json"
GOLD = HERE / "data" / "gold" / "market_prices_combined.json"
EMMSA_URL = "https://www.emmsa.com.pe/index.php/precios-diarios/"

EMMSA_PRODUCTS = {
    "ajo": "ajo", "apio": "apio", "arveja": "arveja grano verde",
    "betarraga": "beterraga", "camote": "camote", "cebolla": "cebolla",
    "chirimoya": "chirimoya", "choclo": "choclo", "culantro": "culantro",
    "durazno": "melocoton", "melocoton": "melocoton", "esparrago": "esparrago",
    "espinaca": "espinaca", "fresa": "fresa", "granadilla": "granadilla",
    "haba": "haba grano verde", "hortalizas chinas": "hortalizas chinas",
    "lechuga": "lechuga", "lenteja": "lenteja grano seco", "limon": "limon",
    "maiz": "maiz seco", "mandarina": "mandarina", "mango": "mango",
    "manzana": "manzana", "maracuya": "maracuya", "melon": "melon",
    "membrillo": "membrillo", "naranja": "naranja", "olluco": "olluco",
    "otras frutas": "otros fruticolas", "palta": "palta", "papa": "papa",
    "papaya": "papaya", "pepino": "pepino", "pimiento": "pimiento fresco",
    "pina": "pina", "platanos": "platano", "sandia": "sandia",
    "tomate": "tomate", "tuna": "tuna", "uva": "uva", "vainita": "vainita",
    "yuca": "yuca", "zanahoria": "zanahoria", "zapallo": "zapallo",
}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def emmsa_key(product: object, variety: object) -> str | None:
    product_norm, variety_norm = norm(product), norm(variety)
    if product_norm == "aji":
        return "aji seco" if any(token in variety_norm for token in ("seco", "panca", "paprika")) else "aji fresco"
    if product_norm == "frejol":
        return "frijol grano verde" if "verde" in variety_norm else "frijol grano seco"
    return EMMSA_PRODUCTS.get(product_norm)


def variety_key(value: object) -> str:
    return norm(value) or "sin variedad"


def daily_summary(group: pd.DataFrame) -> dict:
    daily = group.groupby("date", as_index=False).agg(
        price_min=("price_min", "min"), price_avg=("price_avg", "mean"),
        price_max=("price_max", "max"), volume_t=("volume_t", "sum"),
    ).sort_values("date")
    valid = daily[daily["price_avg"].notna()]
    latest = valid.iloc[-1] if not valid.empty else daily.iloc[-1]
    latest_date = latest["date"]
    prior = valid[valid["date"] <= latest_date - timedelta(days=7)]
    prior_row = prior.iloc[-1] if not prior.empty else None
    week = valid[valid["date"] >= latest_date - timedelta(days=6)]["price_avg"].mean()
    month = valid[valid["date"] >= latest_date - timedelta(days=29)]["price_avg"].mean()
    price = None if pd.isna(latest["price_avg"]) else round(float(latest["price_avg"]), 2)
    price_7d = None if prior_row is None or pd.isna(prior_row["price_avg"]) else round(float(prior_row["price_avg"]), 2)
    return {
        "latest_date": latest_date.strftime("%Y-%m-%d"), "current_price": price,
        "current_min": None if pd.isna(latest["price_min"]) else round(float(latest["price_min"]), 2),
        "current_max": None if pd.isna(latest["price_max"]) else round(float(latest["price_max"]), 2),
        "current_volume_t": None if pd.isna(latest["volume_t"]) else round(float(latest["volume_t"]), 2),
        "price_7d": price_7d,
        "price_7d_date": None if prior_row is None else prior_row["date"].strftime("%Y-%m-%d"),
        "price_week": None if pd.isna(week) else round(float(week), 2),
        "price_month": None if pd.isna(month) else round(float(month), 2),
        "change_7d": None if not price_7d or price is None else round(price / price_7d - 1, 4),
        "series": [
            {"date": row.date.strftime("%Y-%m-%d"),
             "price": None if pd.isna(row.price_avg) else round(float(row.price_avg), 2),
             "volume_t": None if pd.isna(row.volume_t) else round(float(row.volume_t), 2)}
            for row in daily.tail(30).itertuples()
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--precios-root")
    args = parser.parse_args()
    project = find_project(args.precios_root)
    catalog, selected = build_purchase_catalog()

    sisap_files = sorted((project / "Arquitectura Medallón" / "Output" / "silver" / "sisap_mercados").glob("anio=*/data.parquet"))
    emmsa_file = project / "Output" / "EMMSA" / "Data_Precio_Volumen_EMMSA.csv"
    if not sisap_files or not emmsa_file.exists() or not SISAP_GOLD.exists():
        raise FileNotFoundError("Faltan las fuentes Silver SISAP, EMMSA o el contrato Gold SISAP")

    sisap = pd.concat([pd.read_parquet(path) for path in sisap_files[-2:]], ignore_index=True)
    sisap["date"] = pd.to_datetime(sisap["fecha"], errors="coerce")
    sisap["product_key"] = sisap["producto_norm"].map(norm)
    sisap = sisap[sisap["product_key"].isin(selected)].copy()
    sisap["source"] = "SISAP"
    sisap["market"] = sisap["mercado"].astype(str)
    sisap["variety"] = sisap["variedad"].fillna("Sin variedad").astype(str)
    sisap["price_min"] = pd.to_numeric(sisap["precio_min"], errors="coerce")
    sisap["price_avg"] = pd.to_numeric(sisap["precio_promedio"], errors="coerce")
    sisap["price_max"] = pd.to_numeric(sisap["precio_max"], errors="coerce")
    sisap["volume_t"] = pd.to_numeric(sisap["volumen_t"], errors="coerce")

    emmsa = pd.read_csv(emmsa_file, low_memory=False)
    emmsa["date"] = pd.to_datetime(emmsa["Fecha"], errors="coerce")
    emmsa["product_key"] = [emmsa_key(p, v) for p, v in zip(emmsa["Producto"], emmsa["Variedad"])]
    emmsa = emmsa[emmsa["product_key"].isin(selected)].copy()
    emmsa["source"], emmsa["market"] = "EMMSA", "Gran Mercado Mayorista de Lima"
    emmsa["variety"] = emmsa["Variedad"].fillna("Sin variedad").astype(str).str.title()
    for target, source in (("price_min", "Precio_Min"), ("price_avg", "Precio_Prom"),
                           ("price_max", "Precio_Max"), ("volume_t", "Volumen")):
        emmsa[target] = pd.to_numeric(emmsa[source], errors="coerce")

    columns = ["date", "product_key", "source", "market", "variety", "price_min", "price_avg", "price_max", "volume_t"]
    combined = pd.concat([sisap[columns], emmsa[columns]], ignore_index=True)
    combined = combined[combined["date"].notna()].copy()
    combined["variety_key"] = combined["variety"].map(variety_key)

    summaries = []
    for (product, source), group in combined.groupby(["product_key", "source"]):
        summaries.append({"product_key": product, "product": product_label(product), "source": source,
                          "variety_key": "__all__", "variety": "Todas las variedades", **daily_summary(group)})
    for (product, source, vkey), group in combined.groupby(["product_key", "source", "variety_key"]):
        labels = group["variety"].mode()
        summaries.append({"product_key": product, "product": product_label(product), "source": source,
                          "variety_key": vkey, "variety": labels.iloc[0] if not labels.empty else vkey.title(),
                          **daily_summary(group)})

    sisap_gold = json.loads(SISAP_GOLD.read_text(encoding="utf-8"))
    latest_records = [{**row, "source": "SISAP", "product_key": norm(row["product"]),
                       "variety_key": variety_key(row.get("variety"))} for row in sisap_gold.get("records", [])]
    emmsa_latest = emmsa[emmsa["date"] == emmsa["date"].max()]
    latest_records.extend({
        "source": "EMMSA", "product_key": row.product_key, "product": product_label(row.product_key),
        "variety_key": variety_key(row.variety), "variety": row.variety,
        "date": row.date.strftime("%d/%m/%Y"), "market": row.market,
        "origin_country": "", "origin_department": "", "origin_province": "No informado por EMMSA",
        "origin_quality": "not_provided_by_source",
        "price_min": None if pd.isna(row.price_min) else round(float(row.price_min), 2),
        "price_avg": None if pd.isna(row.price_avg) else round(float(row.price_avg), 2),
        "price_max": None if pd.isna(row.price_max) else round(float(row.price_max), 2),
        "volume_t": None if pd.isna(row.volume_t) else round(float(row.volume_t), 2),
    } for row in emmsa_latest.itertuples())

    now = datetime.now(ZoneInfo("America/Lima"))
    BRONZE.parent.mkdir(parents=True, exist_ok=True)
    bronze = {
        "layer": "bronze_reference", "captured_at": now.isoformat(timespec="minutes"),
        "sources": [
            {"source": "SISAP", "logical_path": str(sisap_files[-1].relative_to(project)), "sha256": digest(sisap_files[-1]),
             "url": sisap_gold.get("source_url")},
            {"source": "EMMSA", "logical_path": str(emmsa_file.relative_to(project)), "sha256": digest(emmsa_file), "url": EMMSA_URL},
        ],
        "note": "Los archivos crudos permanecen en el proyecto local Precios Mayoristas; no se publican en GitHub.",
    }
    BRONZE.write_text(json.dumps(bronze, ensure_ascii=False, indent=2), encoding="utf-8")
    SILVER.parent.mkdir(parents=True, exist_ok=True)
    silver = {
        "layer": "silver_catalog", "generated_at": now.isoformat(timespec="minutes"),
        "selected_products": [{"product_key": key, "product": product_label(key)} for key in sorted(selected)],
        "source_crosswalk": {"EMMSA": EMMSA_PRODUCTS},
        "quality": {"input_items": len(catalog), "matched_items": sum(bool(x["sisap_product_key"]) for x in catalog),
                    "selected_products": len(selected), "emmsa_latest_date": emmsa["date"].max().strftime("%Y-%m-%d"),
                    "sisap_latest_date": sisap["date"].max().strftime("%Y-%m-%d")},
        "lineage": str(BRONZE.relative_to(HERE)),
    }
    SILVER.write_text(json.dumps(silver, ensure_ascii=False, indent=2), encoding="utf-8")
    gold = {
        "contract_version": 1, "layer": "gold", "generated_at": now.isoformat(timespec="minutes"),
        "sources": {"SISAP": sisap["date"].max().strftime("%Y-%m-%d"),
                    "EMMSA": emmsa["date"].max().strftime("%Y-%m-%d")},
        "summaries": summaries, "latest_records": latest_records,
        "quality": {**silver["quality"], "summary_rows": len(summaries), "latest_rows": len(latest_records)},
        "lineage": {"bronze": str(BRONZE.relative_to(HERE)), "silver": str(SILVER.relative_to(HERE))},
        "disclaimer": "EMMSA y SISAP son fuentes de mercado. Sus precios pueden diferir por fecha, unidad, variedad y metodología.",
    }
    GOLD.write_text(json.dumps(gold, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"OK {GOLD}: {len(summaries)} resúmenes, {len(latest_records)} filas vigentes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
