"""Exporta un contrato Gold compacto de abastecimiento pesquero.

La fuente completa vive en el proyecto hermano ``Precios Mayoristas``. En
GitHub Actions esa carpeta no existe, por lo que se conserva el último Gold
versionado y el dashboard continúa funcionando.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from pathlib import Path

from common import DATA_DIR, ROOT, banner

DEFAULT_SOURCE = ROOT.parents[1] / "Precios Mayoristas" / "Output" / "PESCADOS" / "PESCADOS_Precios_Historico.csv"
SOURCE = Path(os.environ.get("PESCADOS_SOURCE", DEFAULT_SOURCE))
GOLD = DATA_DIR / "gold" / "fish_supply.json"
MAPPING = DATA_DIR / "gold" / "fish_provenance_mapping.csv"

CAPTAINCIES = {
    "Zorritos": ("Norte", ["ZORRITOS", "PUERTO PIZARRO", "TUMBES", "CANCAS", "LA CRUZ", "PUNTA SAL", "ACAPULCO"]),
    "Talara": ("Norte", ["TALARA", "MANCORA", "LOBITOS", "LOS ORGANOS", "CABO BLANCO", "EL NURO"]),
    "Paita": ("Norte", ["PAITA", "SECHURA", "BAYOVAR", "BAYOBAR", "COLAN", "PARACHIQUE", "PIURA"]),
    "Pimentel": ("Norte", ["PIMENTEL", "CHICLAYO", "SANTA ROSA", "ETEN", "LAMBAYEQUE", "SAN JOSE", "SANJOSE"]),
    "Salaverry": ("Norte", ["SALAVERRY", "TRUJILLO", "PACASMAYO", "HUANCHACO", "MALABRIGO"]),
    "Chimbote": ("Norte", ["CHIMBOTE", "COISHCO", "SAMANCO", "CASMA"]),
    "Supe": ("Centro", ["SUPE", "HUARMEY", "PARAMONGA", "BARRANCA"]),
    "Huacho": ("Centro", ["HUACHO", "HUAURA", "VEGUETA", "CARQUIN"]),
    "Chancay": ("Centro", ["CHANCAY"]),
    "Callao": ("Centro", ["CALLAO", "VENTANILLA", "CHORRILLOS", "PUCUSANA", "ANCON"]),
    "Pisco": ("Centro", ["PISCO", "PARACAS", "SAN ANDRES", "CERRO AZUL", "TAMBO DE MORA", "LAGUNILLA", "PUNTILLA", "ICA"]),
    "San Juan": ("Sur", ["SAN JUAN", "MARCONA", "NAZCA", "CHALA", "LOMAS"]),
    "Mollendo": ("Sur", ["MOLLENDO", "MATARANI", "ISLAY", "ISALY", "ATICO", "QUILCA", "AREQUIPA", "PLANCHADA", "LA PLANCHADA"]),
    "Ilo": ("Sur", ["ILO", "VILA VILA", "MORRO SAMA", "TACNA", "MOQUEGUA", "VITE"]),
}


def map_provenance(value: str) -> tuple[str, str, str]:
    text = (value or "").upper().strip()
    if any(word in text for word in ("IMPORTADO", "OTRO PAIS", "ECUADOR", "CHILE")):
        return "Importado", "Internacional", "importado"
    hits = [(cap, zone) for cap, (zone, aliases) in CAPTAINCIES.items() if any(text == alias or text.startswith(alias + " ") or text.endswith(" " + alias) for alias in aliases)]
    if len(hits) == 1:
        return hits[0][0], hits[0][1], "regla_alias"
    if len(hits) > 1:
        return "Revisión", "Sin zona", "ambiguo"
    return "Sin mapear", "Sin zona", "sin_coincidencia"


def weighted_price(frame):
    valid = frame[frame["Precio_Prom"].notna() & frame["Volumen_kg"].gt(0)]
    if valid.empty:
        return None
    return float((valid["Precio_Prom"] * valid["Volumen_kg"]).sum() / valid["Volumen_kg"].sum())


def summarize_origins(frame, limit: int = 14):
    origins = frame[~frame["Capitania"].isin(["Sin mapear", "Revisión", "Importado"])].groupby(
        ["Capitania", "Zona"]
    )["Volumen_kg"].sum().sort_values(ascending=False)
    total = float(origins.sum())
    rows = [
        {"captaincy": cap, "zone": zone, "volume_kg": round(float(volume), 1),
         "share": round(float(volume / total), 5) if total else 0}
        for (cap, zone), volume in origins.head(limit).items()
    ]
    return rows, total


def main() -> int:
    banner("Exportando abastecimiento pesquero Gold")
    if not SOURCE.exists():
        print(f"  Fuente local no disponible; se conserva {GOLD}")
        return 0 if GOLD.exists() else 1
    import pandas as pd

    cols = ["Fecha", "Mercado", "Especie", "Ambito", "Volumen_kg", "Precio_Prom", "Lugar_Procedencia_norm"]
    data = pd.read_csv(SOURCE, usecols=cols, low_memory=False)
    data["Fecha"] = pd.to_datetime(data["Fecha"], errors="coerce")
    today = pd.Timestamp.now().normalize()
    data = data[data["Fecha"].notna() & data["Fecha"].le(today) & data["Ambito"].astype(str).str.upper().eq("MARITIMO")].copy()
    data["Volumen_kg"] = pd.to_numeric(data["Volumen_kg"], errors="coerce").fillna(0.0)
    data["Precio_Prom"] = pd.to_numeric(data["Precio_Prom"], errors="coerce")
    mapped = data["Lugar_Procedencia_norm"].fillna("").map(map_provenance)
    data[["Capitania", "Zona", "Metodo_Mapeo"]] = pd.DataFrame(mapped.tolist(), index=data.index)

    mapping = data.groupby(["Lugar_Procedencia_norm", "Capitania", "Zona", "Metodo_Mapeo"], dropna=False).agg(
        Registros=("Fecha", "size"), Volumen_kg=("Volumen_kg", "sum"), Primera_Fecha=("Fecha", "min"), Ultima_Fecha=("Fecha", "max")
    ).reset_index().sort_values("Volumen_kg", ascending=False)
    mapping["Participacion_Volumen"] = mapping["Volumen_kg"] / data["Volumen_kg"].sum()
    MAPPING.parent.mkdir(parents=True, exist_ok=True)
    mapping.to_csv(MAPPING, index=False, encoding="utf-8-sig", date_format="%Y-%m-%d")

    latest = data["Fecha"].max()
    recent = data[data["Fecha"].ge(latest - pd.Timedelta(days=364))]
    top_species = recent.groupby("Especie")["Volumen_kg"].sum().nlargest(100).index
    products = []
    for species in top_species:
        all_s = data[data["Especie"].eq(species)]
        s = recent[recent["Especie"].eq(species)]
        species_latest = all_s["Fecha"].max()
        current_rows = all_s[all_s["Fecha"].eq(species_latest)]
        target_7d = species_latest - pd.Timedelta(days=7)
        previous_dates = all_s.loc[all_s["Fecha"].le(target_7d), "Fecha"]
        previous_date = previous_dates.max() if not previous_dates.empty else None
        previous_rows = all_s[all_s["Fecha"].eq(previous_date)] if previous_date is not None else all_s.iloc[0:0]
        week_rows = all_s[all_s["Fecha"].ge(species_latest - pd.Timedelta(days=6))]
        month_rows = all_s[all_s["Fecha"].ge(species_latest - pd.Timedelta(days=29))]
        last30 = all_s[all_s["Fecha"].gt(latest - pd.Timedelta(days=30))]
        prev30 = all_s[(all_s["Fecha"].gt(latest - pd.Timedelta(days=60))) & (all_s["Fecha"].le(latest - pd.Timedelta(days=30)))]
        origin_rows, mapped_total = summarize_origins(s, 12)
        origins_daily, _ = summarize_origins(current_rows)
        origins_week, _ = summarize_origins(week_rows)
        origins_month, _ = summarize_origins(month_rows)
        markets = s.groupby("Mercado")["Volumen_kg"].sum().sort_values(ascending=False)
        market_total = float(markets.sum())
        market_rows = [{"market": str(name), "volume_kg": round(float(vol), 1), "share": round(float(vol / market_total), 5) if market_total else 0} for name, vol in markets.head(8).items()]
        price30, price_prev = weighted_price(last30), weighted_price(prev30)
        vol30, vol_prev = float(last30["Volumen_kg"].sum()), float(prev30["Volumen_kg"].sum())
        hhi = sum(row["share"] ** 2 for row in origin_rows)
        products.append({
            "species": str(species), "volume_365_kg": round(float(s["Volumen_kg"].sum()), 1),
            "volume_30_kg": round(vol30, 1), "volume_change_30d": round(vol30 / vol_prev - 1, 4) if vol_prev else None,
            "price_30": round(price30, 2) if price30 is not None else None,
            "price_change_30d": round(price30 / price_prev - 1, 4) if price30 and price_prev else None,
            "produce_latest_date": species_latest.date().isoformat(),
            "produce_current_price": round(weighted_price(current_rows), 2) if weighted_price(current_rows) is not None else None,
            "produce_price_7d": round(weighted_price(previous_rows), 2) if weighted_price(previous_rows) is not None else None,
            "produce_price_7d_date": previous_date.date().isoformat() if previous_date is not None else None,
            "produce_price_week": round(weighted_price(week_rows), 2) if weighted_price(week_rows) is not None else None,
            "produce_price_month": round(weighted_price(month_rows), 2) if weighted_price(month_rows) is not None else None,
            "produce_current_volume_kg": round(float(current_rows["Volumen_kg"].sum()), 1),
            "produce_volume_7d_kg": round(float(previous_rows["Volumen_kg"].sum()), 1) if previous_date is not None else None,
            "produce_volume_month_kg": round(float(month_rows["Volumen_kg"].sum()), 1),
            "origins_daily": origins_daily, "origins_week": origins_week, "origins_month": origins_month,
            "mapped_share": round(mapped_total / float(s["Volumen_kg"].sum()), 5) if s["Volumen_kg"].sum() else 0,
            "concentration_hhi": round(hhi, 4), "origins": origin_rows, "markets": market_rows,
        })

    mapped_volume = data.loc[~data["Capitania"].isin(["Sin mapear", "Revisión", "Importado"]), "Volumen_kg"].sum()
    contract = {
        "contract_version": 1, "layer": "gold", "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_file": SOURCE.name, "data_from": data["Fecha"].min().date().isoformat(), "data_to": latest.date().isoformat(),
        "rows": int(len(data)), "species_count": int(data["Especie"].nunique()), "market_count": int(data["Mercado"].nunique()),
        "provenance_count": int(data["Lugar_Procedencia_norm"].nunique()), "mapped_volume_share": round(float(mapped_volume / data["Volumen_kg"].sum()), 5),
        "products": products,
    }
    GOLD.write_text(json.dumps(contract, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  -> {GOLD} ({len(products)} especies)")
    print(f"  -> {MAPPING} ({len(mapping)} procedencias; {contract['mapped_volume_share']:.1%} del volumen mapeado)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
