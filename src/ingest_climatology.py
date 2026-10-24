"""Ingesta de climatologia de referencia (ERA5 via Open-Meteo Archive).

Objetivo: para cada punto, construir la distribucion historica 1991-2020 de
lluvia diaria y derivar umbrales. Se calculan dos familias:

  - Umbrales de 1 dia (p90/p95/p99 de dias humedos): que tan raro es un dia.
  - Umbrales de 3 dias (suma movil): cuanta agua acumula el suelo antes de
    activar una quebrada. En la costa peruana la saturacion antecedente pesa
    tanto como el pico diario.

Modos:
    python ingest_climatology.py             # descarga (usa cache) y calcula
    python ingest_climatology.py --refresh   # fuerza redescarga
    python ingest_climatology.py --from-db   # recalcula umbrales sin descargar
"""

from __future__ import annotations

import sys
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np

from common import (
    banner,
    db,
    http_json,
    load_points,
    set_meta,
    sync_points,
)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
SPAN = ("1991-01-01", "2020-12-31")
SOURCE = "ERA5 (Open-Meteo Archive) 1991-2020"
WET_THRESHOLD_MM = 1.0       # dia humedo
WET3_THRESHOLD_MM = 3.0      # 3 dias con algo de lluvia
MIN_SAMPLES = 20             # por debajo de esto el percentil no es informativo


def fetch_point(point: dict, refresh: bool = False) -> tuple[str, list[dict]]:
    query = urllib.parse.urlencode(
        {
            "latitude": point["lat"],
            "longitude": point["lon"],
            "start_date": SPAN[0],
            "end_date": SPAN[1],
            "daily": "precipitation_sum",
            "timezone": "America/Lima",
        }
    )
    payload = http_json(
        f"{ARCHIVE_URL}?{query}",
        timeout=180,
        retries=6,
        use_cache=True,
        max_age_hours=None if not refresh else -1,
    )
    daily = payload.get("daily") or {}
    rows = [
        {"point_id": point["id"], "date": d, "precip_mm": float(v) if v is not None else 0.0}
        for d, v in zip(daily.get("time", []), daily.get("precipitation_sum", []))
    ]
    rows.sort(key=lambda r: r["date"])
    return point["id"], rows


def _pct(arr: np.ndarray, q: float) -> float | None:
    if arr.size < MIN_SAMPLES:
        return None
    return float(np.percentile(arr, q))


def compute_thresholds(rows: list[dict]) -> dict:
    values = np.asarray([r["precip_mm"] for r in rows], dtype=float)
    values = values[~np.isnan(values)]
    years = values.size / 365.25 if values.size else 0.0
    wet = values[values >= WET_THRESHOLD_MM]

    # Suma movil de 3 dias (se recorta el arranque para mantener la comparacion).
    if values.size >= 3:
        roll3 = values[:-2] + values[1:-1] + values[2:]
    else:
        roll3 = np.array([])
    wet3 = roll3[roll3 >= WET3_THRESHOLD_MM] if roll3.size else np.array([])

    return {
        "n_days": int(values.size),
        "years": round(years, 2),
        "wet_days_per_year": round(wet.size / years, 1) if years else 0.0,
        "mean_annual_mm": round(float(values.sum()) / years, 1) if years else 0.0,
        "p50_wet": _pct(wet, 50),
        "p90_wet": _pct(wet, 90),
        "p95_wet": _pct(wet, 95),
        "p99_wet": _pct(wet, 99),
        "p95_3d_wet": _pct(wet3, 95),
        "p99_3d_wet": _pct(wet3, 99),
        "p99_all": float(np.percentile(values, 99)) if values.size else None,
        "max_daily": float(values.max()) if values.size else None,
        "source": SOURCE,
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


THRESHOLD_SQL = """INSERT OR REPLACE INTO thresholds
   (point_id, n_days, years, wet_days_per_year, mean_annual_mm,
    p50_wet, p90_wet, p95_wet, p99_wet, p95_3d_wet, p99_3d_wet,
    p99_all, max_daily, source, computed_at)
   VALUES (:point_id, :n_days, :years, :wet_days_per_year, :mean_annual_mm,
           :p50_wet, :p90_wet, :p95_wet, :p99_wet, :p95_3d_wet, :p99_3d_wet,
           :p99_all, :max_daily, :source, :computed_at)"""


def load_from_db() -> dict[str, list[dict]]:
    with db() as conn:
        rows = [
            {"point_id": r["point_id"], "date": r["date"], "precip_mm": r["precip_mm"]}
            for r in conn.execute(
                "SELECT point_id, date, precip_mm FROM climatology ORDER BY point_id, date"
            )
        ]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["point_id"]].append(row)
    return dict(grouped)


def main() -> int:
    refresh = "--refresh" in sys.argv
    from_db = "--from-db" in sys.argv
    points = load_points()

    if from_db:
        banner("Recalculando umbrales desde la climatologia ya cargada")
        results = load_from_db()
        if not results:
            print("  La tabla climatology esta vacia. Corre la ingesta normal.")
            return 1
        print(f"  Puntos con climatologia: {len(results)}")
    else:
        banner(f"Climatologia ERA5 1991-2020  |  {len(points)} puntos")
        results = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(fetch_point, p, refresh): p for p in points}
            for i, fut in enumerate(as_completed(futures), 1):
                point = futures[fut]
                try:
                    pid, rows = fut.result()
                    results[pid] = rows
                    print(f"  [{i}/{len(points)}] {point['name']:<16} {len(rows):>6} dias")
                except Exception as exc:  # noqa: BLE001
                    print(f"  [{i}/{len(points)}] {point['name']:<16} ERROR: {exc}")

    with db() as conn:
        sync_points(conn, points)
        if not from_db:
            conn.execute("DELETE FROM climatology")
            for point in points:
                rows = results.get(point["id"])
                if not rows:
                    continue
                conn.executemany(
                    "INSERT OR REPLACE INTO climatology (point_id, date, precip_mm) "
                    "VALUES (?, ?, ?)",
                    [(r["point_id"], r["date"], r["precip_mm"]) for r in rows],
                )

        conn.execute("DELETE FROM thresholds")
        for point in points:
            rows = results.get(point["id"])
            if not rows:
                continue
            th = compute_thresholds(rows)
            th["point_id"] = point["id"]
            conn.execute(THRESHOLD_SQL, th)

        set_meta(conn, "climatology_run_utc", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        set_meta(conn, "climatology_source", SOURCE)
        total = conn.execute("SELECT COUNT(*) FROM climatology").fetchone()[0]
        n_th = conn.execute("SELECT COUNT(*) FROM thresholds").fetchone()[0]

    print(f"\n  Filas de climatologia: {total:,}  |  Umbrales: {n_th}/{len(points)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
