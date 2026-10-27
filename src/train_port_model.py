"""Entrena un modelo histórico de restricción/cierre de puertos.

Usa el panel DICAPI, oleaje Copernicus y viento histórico que ya existen
en el proyecto hermano ``Precios Mayoristas``. El artefacto Gold es JSON
para que GitHub Pages pueda puntuar sin dependencias de ML en producción.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from common import DATA_DIR, banner


ROOT = Path(__file__).resolve().parents[3] / "Precios Mayoristas"
STATE = ROOT / "Output" / "DICAPI" / "DICAPI_Estado_Horarios_2022_actual.csv"
WAVES = ROOT / "Output" / "OLAS" / "OLAS_Entrenamiento_Puertos_2022_actual.csv"
WIND = ROOT / "Output" / "VIENTOS" / "VIENTOS_Historico_Puertos.csv"
TARGET = DATA_DIR / "gold" / "port_closure_model.json"


def circular_mean(values: pd.Series) -> tuple[float, float]:
    radians = np.deg2rad(pd.to_numeric(values, errors="coerce"))
    return float(np.nanmean(np.sin(radians))), float(np.nanmean(np.cos(radians)))


def load_daily() -> pd.DataFrame:
    banner("Preparando dataset histórico de puertos")
    state = pd.read_csv(
        STATE,
        usecols=["Fecha", "Puerto_ID_DICAPI", "Capitania", "Estado_ID", "Estado_Conocido"],
        dtype={"Puerto_ID_DICAPI": str},
    )
    state = state[state["Estado_Conocido"].eq("SI") & state["Estado_ID"].notna()].copy()
    states = state.groupby(["Fecha", "Puerto_ID_DICAPI", "Capitania"], as_index=False).agg(
        max_level=("Estado_ID", "max")
    )

    wave_cols = [
        "Fecha", "Puerto_ID_DICAPI", "Altura_Significativa_m", "Periodo_Pico_s",
        "Direccion_Media_grados",
    ]
    waves = pd.read_csv(WAVES, usecols=wave_cols, dtype={"Puerto_ID_DICAPI": str})
    angle = np.deg2rad(pd.to_numeric(waves["Direccion_Media_grados"], errors="coerce"))
    waves["wave_dir_sin"] = np.sin(angle)
    waves["wave_dir_cos"] = np.cos(angle)
    waves = waves.groupby(["Fecha", "Puerto_ID_DICAPI"], as_index=False).agg(
        wave_height=("Altura_Significativa_m", "max"),
        wave_period=("Periodo_Pico_s", "max"),
        wave_dir_sin=("wave_dir_sin", "mean"),
        wave_dir_cos=("wave_dir_cos", "mean"),
    )

    wind = pd.read_csv(
        WIND,
        usecols=["Fecha_Hora_Local", "Puerto_ID_DICAPI", "Viento_ms", "Direccion_Viento_grados"],
        dtype={"Puerto_ID_DICAPI": str},
    )
    wind["Fecha"] = wind["Fecha_Hora_Local"].str.slice(0, 10)
    angle = np.deg2rad(pd.to_numeric(wind["Direccion_Viento_grados"], errors="coerce"))
    wind["wind_dir_sin"] = np.sin(angle)
    wind["wind_dir_cos"] = np.cos(angle)
    wind = wind.groupby(["Fecha", "Puerto_ID_DICAPI"], as_index=False).agg(
        wind_speed=("Viento_ms", "max"),
        wind_dir_sin=("wind_dir_sin", "mean"),
        wind_dir_cos=("wind_dir_cos", "mean"),
    )

    frame = states.merge(waves, on=["Fecha", "Puerto_ID_DICAPI"], how="inner")
    frame = frame.merge(wind, on=["Fecha", "Puerto_ID_DICAPI"], how="inner")
    frame["date"] = pd.to_datetime(frame["Fecha"], errors="coerce")
    frame["restriction"] = frame["max_level"].ge(2).astype(float)
    frame["closure"] = frame["max_level"].ge(5).astype(float)
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    print(
        f"  -> {len(frame):,} puerto-días, {frame.Puerto_ID_DICAPI.nunique()} puertos, "
        f"{frame.date.min().date()} a {frame.date.max().date()}"
    )
    return frame


def feature_frame(frame: pd.DataFrame, captaincies: list[str]) -> tuple[np.ndarray, list[str]]:
    month = frame["date"].dt.month.to_numpy(float)
    wave = frame["wave_height"].to_numpy(float)
    period = frame["wave_period"].to_numpy(float)
    wind = frame["wind_speed"].to_numpy(float)
    columns = [
        wave, period, wind, wave * wave, wind * wind, wave * wind,
        frame["wave_dir_sin"].to_numpy(float), frame["wave_dir_cos"].to_numpy(float),
        frame["wind_dir_sin"].to_numpy(float), frame["wind_dir_cos"].to_numpy(float),
        np.sin(2 * np.pi * month / 12), np.cos(2 * np.pi * month / 12),
        frame["port_restriction_prior"].to_numpy(float),
        frame["port_closure_prior"].to_numpy(float),
    ]
    names = [
        "wave_height", "wave_period", "wind_speed", "wave_height_sq", "wind_speed_sq",
        "wave_x_wind", "wave_dir_sin", "wave_dir_cos", "wind_dir_sin", "wind_dir_cos",
        "month_sin", "month_cos",
        "port_restriction_prior", "port_closure_prior",
    ]
    for captaincy in captaincies[1:]:
        columns.append(frame["Capitania"].eq(captaincy).to_numpy(float))
        names.append("captaincy=" + captaincy)
    return np.column_stack(columns), names


def sigmoid(value: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(value, -30, 30)))


def fit_logistic(x: np.ndarray, y: np.ndarray, ridge: float = 1.0) -> np.ndarray:
    design = np.column_stack([np.ones(len(x)), x])
    beta = np.zeros(design.shape[1])
    penalty = np.eye(design.shape[1]) * ridge
    penalty[0, 0] = 0
    for _ in range(35):
        probability = sigmoid(design @ beta)
        weight = np.maximum(probability * (1 - probability), 1e-5)
        hessian = design.T @ (weight[:, None] * design) + penalty
        gradient = design.T @ (y - probability) - penalty @ beta
        step = np.linalg.solve(hessian, gradient)
        beta += step
        if np.max(np.abs(step)) < 1e-7:
            break
    return beta


def auc(y: np.ndarray, probability: np.ndarray) -> float:
    order = np.argsort(probability)
    ranks = np.empty(len(order), dtype=float)
    ranks[order] = np.arange(1, len(order) + 1)
    positives = y == 1
    n_pos, n_neg = positives.sum(), (~positives).sum()
    if not n_pos or not n_neg:
        return 0.5
    return float((ranks[positives].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def metrics(y: np.ndarray, probability: np.ndarray, threshold: float) -> dict:
    predicted = probability >= threshold
    positive = y == 1
    tp = int((predicted & positive).sum())
    fp = int((predicted & ~positive).sum())
    fn = int((~predicted & positive).sum())
    tn = int((~predicted & ~positive).sum())
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    specificity = tn / max(tn + fp, 1)
    return {
        "threshold": round(float(threshold), 3), "auc": round(auc(y, probability), 4),
        "brier": round(float(np.mean((probability - y) ** 2)), 4),
        "precision": round(precision, 4), "recall": round(recall, 4),
        "specificity": round(specificity, 4),
        "f1": round(2 * precision * recall / max(precision + recall, 1e-12), 4),
        "support": int(positive.sum()), "samples": len(y),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
    }


def choose_threshold(y: np.ndarray, probability: np.ndarray) -> tuple[float, dict]:
    candidates = np.arange(0.05, 0.81, 0.01)
    scored = [(threshold, metrics(y, probability, threshold)) for threshold in candidates]
    threshold, result = max(scored, key=lambda item: (item[1]["f1"], item[1]["specificity"]))
    return float(threshold), result


def train_target(
    train_x: np.ndarray, valid_x: np.ndarray, train_y: np.ndarray, valid_y: np.ndarray,
    mean: np.ndarray, scale: np.ndarray,
) -> tuple[dict, np.ndarray]:
    beta = fit_logistic(train_x, train_y)
    probability = sigmoid(np.column_stack([np.ones(len(valid_x)), valid_x]) @ beta)
    threshold, report = choose_threshold(valid_y, probability)
    raw_coefficients = beta[1:] / scale
    raw_intercept = beta[0] - float(np.sum(beta[1:] * mean / scale))
    return {
        "intercept": raw_intercept,
        "coefficients": raw_coefficients.tolist(),
        "threshold": threshold,
        "validation": report,
        "prevalence_train": round(float(train_y.mean()), 5),
        "prevalence_validation": round(float(valid_y.mean()), 5),
    }, probability


def main() -> int:
    frame = load_daily()
    cutoff = pd.Timestamp("2024-01-01")
    if frame.loc[frame.date >= cutoff, "date"].nunique() < 60:
        cutoff = frame["date"].quantile(0.8).normalize()
    history = frame[frame["date"].lt(cutoff)]
    global_rates = {target: float(history[target].mean()) for target in ("restriction", "closure")}
    prior_table: dict[str, dict[str, float]] = {}
    for port_id, group in history.groupby("Puerto_ID_DICAPI"):
        prior_table[str(port_id)] = {}
        for target in ("restriction", "closure"):
            smoothed = (group[target].sum() + 30 * global_rates[target]) / (len(group) + 30)
            prior_table[str(port_id)][target] = round(float(smoothed), 6)
    frame["port_restriction_prior"] = frame["Puerto_ID_DICAPI"].map(
        {key: value["restriction"] for key, value in prior_table.items()}
    ).fillna(global_rates["restriction"])
    frame["port_closure_prior"] = frame["Puerto_ID_DICAPI"].map(
        {key: value["closure"] for key, value in prior_table.items()}
    ).fillna(global_rates["closure"])
    captaincies = sorted(frame["Capitania"].dropna().unique().tolist())
    x, feature_names = feature_frame(frame, captaincies)
    train_mask = frame["date"].lt(cutoff).to_numpy()
    valid_mask = ~train_mask
    mean = x[train_mask].mean(axis=0)
    scale = x[train_mask].std(axis=0)
    scale[scale < 1e-8] = 1
    standardized = (x - mean) / scale

    models = {}
    probabilities = {}
    for target in ("restriction", "closure"):
        print(f"Entrenando {target}...")
        model, probability = train_target(
            standardized[train_mask], standardized[valid_mask],
            frame.loc[train_mask, target].to_numpy(float),
            frame.loc[valid_mask, target].to_numpy(float), mean, scale,
        )
        models[target] = model
        probabilities[target] = probability
        print("  ->", model["validation"])

    valid = frame.loc[valid_mask]
    heuristic = .55 * np.minimum(valid["wave_height"].to_numpy() / 3, 1) + .45 * np.minimum(valid["wind_speed"].to_numpy() / 20, 1)
    baseline = {
        "restriction": metrics(valid["restriction"].to_numpy(float), heuristic, .38),
        "closure": metrics(valid["closure"].to_numpy(float), heuristic, .55),
    }
    output = {
        "contract_version": 1, "layer": "gold", "model_type": "ridge_logistic_temporal",
        "trained_at": datetime.now(ZoneInfo("America/Lima")).isoformat(timespec="minutes"),
        "training_period": {"from": frame.date.min().date().isoformat(), "to": (cutoff - pd.Timedelta(days=1)).date().isoformat()},
        "validation_period": {"from": cutoff.date().isoformat(), "to": frame.date.max().date().isoformat()},
        "rows": {"total": len(frame), "training": int(train_mask.sum()), "validation": int(valid_mask.sum())},
        "ports": int(frame.Puerto_ID_DICAPI.nunique()), "captaincies": captaincies,
        "global_priors": global_rates, "port_priors": prior_table,
        "features": feature_names, "models": models, "heuristic_baseline": baseline,
        "sources": {"states": str(STATE), "waves": str(WAVES), "wind": str(WIND)},
        "limitations": [
            "La fecha de DICAPI es fecha de publicación; la hora efectiva puede estar en el PDF.",
            "La validación es temporal y observacional; no sustituye la decisión de DICAPI.",
            "El artefacto predice condiciones asociadas a estados históricos, no causalidad.",
        ],
    }
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  -> {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
