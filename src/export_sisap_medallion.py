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
PRODUCTS = {
    "aji fresco", "arroz", "camote", "cebolla", "culantro", "limon",
    "mandarina", "mango", "naranja", "papa", "papaya", "palta", "pina",
    "platano", "tomate",
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
    frame["producto_filtro"] = frame["producto_norm"].map(norm)
    frame = frame[frame["producto_filtro"].isin(PRODUCTS)].copy()

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
            "product": str(row["producto"]),
            "variety": "" if pd.isna(row.get("variedad")) else str(row["variedad"]),
            "price_min": value(row, "precio_min"),
            "price_avg": value(row, "precio_promedio"),
            "price_max": value(row, "precio_max"),
            "volume_t": value(row, "volumen_t"),
            "origin_province": (
                "Procedencia desconocida" if pd.isna(row.get("procedencia")) or not str(row["procedencia"]).strip()
                else str(row["procedencia"])
            ),
            "origin_department": "" if pd.isna(row.get("departamento_procedencia")) else str(row["departamento_procedencia"]),
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
        labels = group["producto"].dropna().astype(str)
        label = labels.mode().iloc[0] if not labels.empty else str(product_key).title()
        volume = group["volumen_t"].sum(min_count=1)
        price = group["precio_promedio"].mean()
        origin_history.append({
            "product_key": str(product_key),
            "product": label,
            "origin_department": "" if pd.isna(department) else str(department),
            "origin_province": str(province),
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
        "contract_version": 2,
        "layer": "gold",
        "source": "SISAP · MIDAGRI vía Arquitectura Medallón",
        "source_url": "http://sistemas.midagri.gob.pe/sisap/portal2/mayorista/",
        "date": max(market_dates.values(), key=lambda x: datetime.strptime(x, "%d/%m/%Y"), default="—"),
        "market_dates": market_dates,
        "fetched_at": now.isoformat(timespec="minutes"),
        "records": records,
        "origin_history": origin_history,
        "history_window": {
            "days": 365,
            "from": history_start.strftime("%d/%m/%Y"),
            "to": latest_date.strftime("%d/%m/%Y"),
        },
        "errors": [],
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
