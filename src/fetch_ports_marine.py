"""Descarga estado DICAPI y pronóstico multimodelo de viento/oleaje."""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from common import DATA_DIR, USER_AGENT, banner


DICAPI = "https://www.dicapi.mil.pe/api/ports"
WEATHER = "https://api.open-meteo.com/v1/forecast"
MARINE = "https://marine-api.open-meteo.com/v1/marine"
TARGET = DATA_DIR / "gold" / "ports_marine.json"
MODEL = DATA_DIR / "gold" / "port_closure_model.json"
INLAND = {"Iquitos", "Pto. Maldonado", "Pucallpa", "Puno", "Yurimaguas"}
WIND_MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless"]
WAVE_MODELS = ["ecmwf_wam025", "ncep_gfswave025"]


def maximum(values):
    clean = [float(value) for value in values if value is not None]
    return max(clean) if clean else 0.0


def circular_components(values):
    clean = [math.radians(float(value)) for value in values if value is not None]
    if not clean:
        return 0.0, 1.0
    return sum(math.sin(value) for value in clean) / len(clean), sum(math.cos(value) for value in clean) / len(clean)


def score_predictions(ports: list[dict], wind: dict, waves: dict):
    if not MODEL.exists():
        return {}, None
    artifact = json.loads(MODEL.read_text(encoding="utf-8"))
    features = artifact["features"]
    predictions = {}
    for port in ports:
        port_predictions = []
        priors = artifact.get("port_priors", {}).get(port["id"], artifact.get("global_priors", {}))
        for index, wave_day in enumerate(waves.get(port["id"], [])):
            wind_days = wind.get(port["id"]) or []
            wind_day = wind_days[index] if index < len(wind_days) else {}
            wave_models = list(wave_day.get("models", {}).values())
            wind_models = list(wind_day.get("models", {}).values())
            wave_height = maximum(item.get("wave_height_max") for item in wave_models)
            wave_period = maximum(item.get("wave_period_max") for item in wave_models)
            wind_speed = maximum(item.get("wind_speed_10m_max") for item in wind_models)
            wave_sin, wave_cos = circular_components(item.get("wave_direction_dominant") for item in wave_models)
            wind_sin, wind_cos = circular_components(item.get("wind_direction_10m_dominant") for item in wind_models)
            month = int(wave_day["date"][5:7])
            values = {
                "wave_height": wave_height, "wave_period": wave_period, "wind_speed": wind_speed,
                "wave_height_sq": wave_height ** 2, "wind_speed_sq": wind_speed ** 2,
                "wave_x_wind": wave_height * wind_speed,
                "wave_dir_sin": wave_sin, "wave_dir_cos": wave_cos,
                "wind_dir_sin": wind_sin, "wind_dir_cos": wind_cos,
                "month_sin": math.sin(2 * math.pi * month / 12),
                "month_cos": math.cos(2 * math.pi * month / 12),
                "port_restriction_prior": priors.get("restriction", artifact["global_priors"]["restriction"]),
                "port_closure_prior": priors.get("closure", artifact["global_priors"]["closure"]),
            }
            values["captaincy=" + port["captaincy"]] = 1.0
            probabilities = {}
            for target_name, model in artifact["models"].items():
                linear = float(model["intercept"]) + sum(
                    float(coefficient) * float(values.get(name, 0.0))
                    for name, coefficient in zip(features, model["coefficients"])
                )
                probabilities[target_name] = 1 / (1 + math.exp(-max(-30, min(30, linear))))
            closure_hit = probabilities["closure"] >= artifact["models"]["closure"]["threshold"]
            restriction_hit = probabilities["restriction"] >= artifact["models"]["restriction"]["threshold"]
            watch = probabilities["restriction"] >= artifact["models"]["restriction"]["threshold"] * 0.65
            signal = "Posible cierre" if closure_hit else "Posible restricción" if restriction_hit else "Vigilancia" if watch else "Operación probable"
            port_predictions.append({
                "date": wave_day["date"], "restriction_probability": round(probabilities["restriction"], 4),
                "closure_probability": round(probabilities["closure"], 4), "signal": signal,
            })
        predictions[port["id"]] = port_predictions
    summary = {
        "type": artifact["model_type"], "trained_at": artifact["trained_at"],
        "training_period": artifact["training_period"], "validation_period": artifact["validation_period"],
        "rows": artifact["rows"], "ports": artifact["ports"],
        "validation": {name: model["validation"] for name, model in artifact["models"].items()},
        "thresholds": {name: model["threshold"] for name, model in artifact["models"].items()},
    }
    return predictions, summary


def get_json(url: str, params: dict | None = None, attempts: int = 3):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)


def batches(items: list[dict], size: int = 20):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def daily_value(daily: dict, variable: str, model: str, index: int):
    values = daily.get(f"{variable}_{model}", daily.get(variable, []))
    value = values[index] if index < len(values) else None
    return None if value is None else round(float(value), 2)


def fetch_models(ports: list[dict], url: str, models: list[str], variables: list[str]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for batch in batches(ports):
        payload = get_json(url, {
            "latitude": ",".join(str(port["lat"]) for port in batch),
            "longitude": ",".join(str(port["lon"]) for port in batch),
            "daily": ",".join(variables), "models": ",".join(models),
            "forecast_days": 15, "timezone": "America/Lima",
            **({"wind_speed_unit": "ms"} if url == WEATHER else {}),
        })
        items = payload if isinstance(payload, list) else [payload]
        for port, item in zip(batch, items):
            daily = item.get("daily", {})
            days = []
            for index, day in enumerate(daily.get("time", [])):
                values = {model: {variable: daily_value(daily, variable, model, index) for variable in variables}
                          for model in models}
                days.append({"date": day, "models": values})
            result[port["id"]] = days
    return result


def main() -> int:
    banner("Descargando estado de puertos y condiciones marinas")
    payload = get_json(DICAPI)
    if payload.get("status") != 0:
        raise RuntimeError("DICAPI devolvió una respuesta inesperada")
    ports = []
    for item in payload.get("data", []):
        coordinates = item.get("coordinates") or {}
        if item.get("captaincy") in INLAND or coordinates.get("lat") is None or coordinates.get("long") is None:
            continue
        ports.append({
            "id": str(item.get("portId", "")), "code": str(item.get("code", "")),
            "name": item.get("port", ""), "captaincy": item.get("captaincy", ""),
            "lat": float(coordinates["lat"]), "lon": float(coordinates["long"]),
            "status": item.get("levelName", "Sin información"),
            "publication_date": item.get("publication_date", ""),
            "updated_at": item.get("updated_at", ""),
            "resolution": item.get("resolution_number", ""),
            "resolution_url": item.get("resolution_url", ""),
        })
    ports.sort(key=lambda x: (x["captaincy"], x["name"]))
    wind = fetch_models(
        ports, WEATHER, WIND_MODELS,
        ["wind_speed_10m_max", "wind_gusts_10m_max", "wind_direction_10m_dominant"],
    )
    waves = fetch_models(
        ports, MARINE, WAVE_MODELS,
        ["wave_height_max", "wave_direction_dominant", "wave_period_max"],
    )
    predictions, model_summary = score_predictions(ports, wind, waves)
    now = datetime.now(ZoneInfo("America/Lima"))
    output = {
        "contract_version": 1, "layer": "gold", "generated_at": now.isoformat(timespec="minutes"),
        "forecast_days": 15, "ports": ports, "wind": wind, "waves": waves,
        "predictions": predictions, "model": model_summary,
        "sources": {"ports": DICAPI, "wind": WEATHER, "waves": MARINE},
        "models": {"wind": WIND_MODELS, "waves": WAVE_MODELS},
    }
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    temporary = TARGET.with_suffix(".tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(TARGET)
    print(f"  -> {TARGET} ({len(ports)} puertos, 15 días)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
