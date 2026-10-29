"""Descarga y resume las observaciones de precios del proveedor pesquero."""
from __future__ import annotations

import csv
import io
import json
import math
import unicodedata
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from common import DATA_DIR, USER_AGENT, banner

SOURCE = "https://docs.google.com/spreadsheets/d/e/2PACX-1vR4RGSyEyVd4ErRfFDGnwWyxKNkcyC_DbRgR0lBkAYiG5ebBVsmnxfXhu3TaxX7H6K_0SHR2q9Q73yl/pub?output=csv"
TARGET = DATA_DIR / "gold" / "fish_provider_prices.json"
PRICE_COLUMNS = (
    "Precio Mínimo  (<05:00 a.m.)", "Precio Máximo  (<05:00 a.m.)",
    "Precio Mínimo (>05:00 a.m.)", "Precio Máximo (>05:00 a.m.)",
)


def key(value: str) -> str:
    text = "".join(c for c in unicodedata.normalize("NFKD", value or "") if not unicodedata.combining(c))
    return " ".join(text.upper().split())


def number(value: str) -> float | None:
    try:
        result = float((value or "").strip().replace(",", "."))
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def weighted(rows: list[dict]) -> float | None:
    valid = [row for row in rows if row["price"] is not None]
    if not valid:
        return None
    quantity = lambda row: row.get("quantity", row.get("quantity_kg", 0.0))
    positive = [row for row in valid if quantity(row) > 0]
    if positive:
        total = sum(quantity(row) for row in positive)
        return sum(row["price"] * quantity(row) for row in positive) / total
    return sum(row["price"] for row in valid) / len(valid)


def main() -> int:
    banner("Descargando precios observados por proveedor")
    request = urllib.request.Request(SOURCE, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            text = response.read().decode("utf-8-sig", "replace")
    except Exception as exc:  # noqa: BLE001
        print(f"  No se pudo actualizar la hoja del proveedor: {exc}")
        return 0 if TARGET.exists() else 1

    grouped: dict[str, dict] = defaultdict(lambda: {"name": "", "days": defaultdict(list)})
    rejected = 0
    for source_row in csv.DictReader(io.StringIO(text)):
        try:
            day = datetime.strptime((source_row.get("Fecha") or "").strip(), "%d/%m/%Y").date()
        except ValueError:
            rejected += 1
            continue
        species = key(source_row.get("Especie", ""))
        prices = [number(source_row.get(column, "")) for column in PRICE_COLUMNS]
        prices = [price for price in prices if price is not None and price > 0]
        if not species or not prices:
            rejected += 1
            continue
        quantity = number(source_row.get("Cantidad (Kg)", "")) or 0.0
        grouped[species]["name"] = " ".join((source_row.get("Especie") or species).strip().split())
        grouped[species]["days"][day].append({
            "price": sum(prices) / len(prices), "low": min(prices), "high": max(prices),
            "quantity": quantity,
        })

    today = datetime.now(ZoneInfo("America/Lima")).date()
    products = []
    for species, group in sorted(grouped.items()):
        daily = []
        for day, rows in sorted(group["days"].items()):
            price = weighted(rows)
            if price is None:
                continue
            daily.append({
                "date": day, "price": price, "low": min(row["low"] for row in rows),
                "high": max(row["high"] for row in rows), "quantity_kg": sum(row["quantity"] for row in rows),
            })
        if not daily:
            continue
        latest = daily[-1]
        target_7d = latest["date"] - timedelta(days=7)
        previous = next((row for row in reversed(daily) if row["date"] <= target_7d), None)
        week = [row for row in daily if row["date"] >= latest["date"] - timedelta(days=6)]
        month = [row for row in daily if row["date"] >= latest["date"] - timedelta(days=29)]
        price_week, price_month = weighted(week), weighted(month)
        products.append({
            "species": group["name"], "species_key": species,
            "latest_date": latest["date"].isoformat(), "staleness_days": max(0, (today - latest["date"]).days),
            "current_price": round(latest["price"], 2), "current_low": round(latest["low"], 2),
            "current_high": round(latest["high"], 2),
            "price_7d": round(previous["price"], 2) if previous else None,
            "price_7d_date": previous["date"].isoformat() if previous else None,
            "price_week": round(price_week, 2) if price_week is not None else None,
            "price_month": round(price_month, 2) if price_month is not None else None,
            "change_7d": round(latest["price"] / previous["price"] - 1, 4) if previous and previous["price"] else None,
            "observations": len(daily),
        })

    output = {
        "contract_version": 1, "layer": "gold", "generated_at": datetime.now(ZoneInfo("America/Lima")).isoformat(timespec="minutes"),
        "source": SOURCE, "method": "Punto medio del rango, ponderado por cantidad disponible",
        "rows": sum(len(group["days"]) for group in grouped.values()), "rejected_rows": rejected,
        "products": products,
    }
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    temporary = TARGET.with_suffix(".tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(TARGET)
    print(f"  -> {TARGET} ({len(products)} especies)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
