"""Ingesta de pronostico operativo.

Tres capas por punto:

1. Deterministico multimodelo (ECMWF IFS, GFS, ICON) - precipitacion horaria 16 dias.
2. Variables de apoyo (humedad relativa, viento, CAPE, humedad del suelo) - best match.
3. Ensemble ECMWF (50 miembros) - probabilidad de excedencia por dia.

La capa 3 es la que importa para decision: un numero unico no comunica riesgo.
"""

from __future__ import annotations

import sys
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from common import (
    banner,
    db,
    http_json,
    load_points,
    set_meta,
    sync_points,
)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
TZ = "America/Lima"
MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless"]
ENSEMBLE_MODEL = "ecmwf_ifs025"
FORECAST_DAYS = 16
ENSEMBLE_DAYS = 15
EXCEEDANCE_MM = (10.0, 25.0, 50.0)

# Ventana de cache en horas; se ajusta a 0 con --refresh para forzar redescarga.
MAX_AGE_H = 3.0


def _q(**kwargs) -> str:
    return urllib.parse.urlencode(kwargs, safe=",")


def fetch_multimodel(point: dict) -> dict[str, list[tuple[str, float]]]:
    url = FORECAST_URL + "?" + _q(
        latitude=point["lat"],
        longitude=point["lon"],
        hourly="precipitation",
        models=",".join(MODELS),
        forecast_days=FORECAST_DAYS,
        timezone=TZ,
    )
    payload = http_json(url, max_age_hours=MAX_AGE_H)
    hourly = payload.get("hourly") or {}
    times = hourly.get("time", [])
    out: dict[str, list[tuple[str, float]]] = {}
    for model in MODELS:
        key = f"precipitation_{model}"
        if key not in hourly:
            continue
        out[model] = [
            (ts, float(v) if v is not None else 0.0)
            for ts, v in zip(times, hourly[key])
        ]
    return out


def fetch_extra(point: dict) -> list[dict]:
    base = dict(
        latitude=point["lat"],
        longitude=point["lon"],
        forecast_days=FORECAST_DAYS,
        timezone=TZ,
    )
    attempts = [
        "relative_humidity_2m,wind_speed_10m,cape,soil_moisture_0_to_7cm",
        "relative_humidity_2m,wind_speed_10m",
    ]
    for variables in attempts:
        try:
            payload = http_json(FORECAST_URL + "?" + _q(hourly=variables, **base), max_age_hours=MAX_AGE_H)
            hourly = payload.get("hourly") or {}
            times = hourly.get("time", [])
            rows = []
            for i, ts in enumerate(times):
                rows.append(
                    {
                        "ts": ts,
                        "rh": hourly.get("relative_humidity_2m", [None] * len(times))[i],
                        "wind": hourly.get("wind_speed_10m", [None] * len(times))[i],
                        "cape": hourly.get("cape", [None] * len(times))[i],
                        "soil_moisture": hourly.get("soil_moisture_0_to_7cm", [None] * len(times))[i],
                    }
                )
            return rows
        except Exception:  # noqa: BLE001
            continue
    return []


def fetch_ensemble(point: dict) -> list[dict]:
    url = ENSEMBLE_URL + "?" + _q(
        latitude=point["lat"],
        longitude=point["lon"],
        hourly="precipitation",
        models=ENSEMBLE_MODEL,
        forecast_days=ENSEMBLE_DAYS,
        timezone=TZ,
    )
    payload = http_json(url, max_age_hours=MAX_AGE_H)
    hourly = payload.get("hourly") or {}
    times = hourly.get("time", [])
    members = sorted(k for k in hourly if k.startswith("precipitation_member"))
    if not times or not members:
        return []

    # Serie diaria por miembro; se descartan dias incompletos (arranque a mitad de dia).
    per_date: dict[str, list[float]] = defaultdict(list)
    member_daily: list[dict[str, float]] = []
    for member in members:
        values = hourly[member]
        by_date_sum: dict[str, float] = defaultdict(float)
        by_date_n: dict[str, int] = defaultdict(int)
        for ts, v in zip(times, values):
            day = ts[:10]
            by_date_sum[day] += float(v) if v is not None else 0.0
            by_date_n[day] += 1
        member_daily.append({d: s for d, s in by_date_sum.items() if by_date_n[d] >= 20})

    dates = sorted({d for md in member_daily for d in md})
    rows = []
    for day in dates:
        sample = np.asarray([md[day] for md in member_daily if day in md], dtype=float)
        if sample.size < 10:
            continue
        row = {
            "date": day,
            "mean_mm": float(sample.mean()),
            "p10_mm": float(np.percentile(sample, 10)),
            "p50_mm": float(np.percentile(sample, 50)),
            "p90_mm": float(np.percentile(sample, 90)),
            "n_members": int(sample.size),
        }
        for thr, key in ((10.0, "prob10"), (25.0, "prob25"), (50.0, "prob50")):
            row[key] = float((sample >= thr).mean())
        rows.append(row)
    return rows


def main() -> int:
    refresh = "--refresh" in sys.argv
    points = load_points()
    run_date = datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d")
    banner(f"Pronostico operativo  |  corrida {run_date}  |  {len(points)} puntos")

    global MAX_AGE_H
    if refresh:
        MAX_AGE_H = 0.0

    det: dict[str, dict] = {}
    extra: dict[str, list] = {}
    ens: dict[str, list] = {}

    def work(point: dict) -> tuple[str, dict, list, list]:
        return (
            point["id"],
            fetch_multimodel(point),
            fetch_extra(point),
            fetch_ensemble(point),
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(work, p): p for p in points}
        for i, fut in enumerate(as_completed(futures), 1):
            point = futures[fut]
            try:
                pid, d, e, s = fut.result()
                det[pid], extra[pid], ens[pid] = d, e, s
                print(
                    f"  [{i}/{len(points)}] {point['name']:<16} "
                    f"modelos={len(d)}  extras={len(e):>3}  ens_dias={len(s):>2}"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  [{i}/{len(points)}] {point['name']:<16} ERROR: {exc}")

    with db() as conn:
        sync_points(conn, points)
        conn.execute("DELETE FROM forecast_hourly WHERE run_date = ?", (run_date,))
        conn.execute("DELETE FROM forecast_extra WHERE run_date = ?", (run_date,))
        conn.execute("DELETE FROM ensemble_daily WHERE run_date = ?", (run_date,))

        for point in points:
            pid = point["id"]
            for model, series in det.get(pid, {}).items():
                conn.executemany(
                    "INSERT OR REPLACE INTO forecast_hourly "
                    "(point_id, run_date, model, ts, precip_mm) VALUES (?, ?, ?, ?, ?)",
                    [(pid, run_date, model, ts, v) for ts, v in series],
                )
            conn.executemany(
                "INSERT OR REPLACE INTO forecast_extra "
                "(point_id, run_date, ts, rh, wind, cape, soil_moisture) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (pid, run_date, r["ts"], r["rh"], r["wind"], r["cape"], r["soil_moisture"])
                    for r in extra.get(pid, [])
                ],
            )
            conn.executemany(
                "INSERT OR REPLACE INTO ensemble_daily "
                "(point_id, run_date, model, date, mean_mm, p10_mm, p50_mm, p90_mm, "
                " prob10, prob25, prob50, n_members) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        pid,
                        run_date,
                        ENSEMBLE_MODEL,
                        r["date"],
                        r["mean_mm"],
                        r["p10_mm"],
                        r["p50_mm"],
                        r["p90_mm"],
                        r["prob10"],
                        r["prob25"],
                        r["prob50"],
                        r["n_members"],
                    )
                    for r in ens.get(pid, [])
                ],
            )

        set_meta(conn, "forecast_run_date", run_date)
        set_meta(conn, "forecast_run_utc", datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"))
        n_h = conn.execute(
            "SELECT COUNT(*) FROM forecast_hourly WHERE run_date = ?", (run_date,)
        ).fetchone()[0]
        n_e = conn.execute(
            "SELECT COUNT(*) FROM ensemble_daily WHERE run_date = ?", (run_date,)
        ).fetchone()[0]

    print(f"\n  Filas horarias: {n_h:,}  |  Filas ensemble: {n_e:,}  |  Corrida: {run_date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
