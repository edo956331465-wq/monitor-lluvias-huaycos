"""Utilidades compartidas: descarga HTTP con cache/reintentos y acceso a SQLite.

Todo el proyecto usa solo la libreria estandar mas numpy, para que corra en
cualquier maquina sin instalar dependencias pesadas.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
OUT_DIR = ROOT / "out"
DB_PATH = DATA_DIR / "warehouse.sqlite"
POINTS_CSV = ROOT / "config" / "points.csv"

USER_AGENT = "MonitorLluviasPeru/0.1 (prototipo de investigacion sin fines comerciales)"

# La consola de Windows suele abrir en cp1252 y revienta con acentos o simbolos.
try:  # pragma: no cover
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# Limitador de tasa global: Open-Meteo devuelve 429 si se le golpea muy seguido.
# Se serializa el inicio de cada peticion con un intervalo minimo.
_MIN_INTERVAL_S = 0.40
_throttle_lock = threading.Lock()
_next_allowed = [0.0]


def _throttle() -> None:
    with _throttle_lock:
        now = time.monotonic()
        wait = _next_allowed[0] - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _next_allowed[0] = now + _MIN_INTERVAL_S

SCHEMA = """
CREATE TABLE IF NOT EXISTS points (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    dept    TEXT,
    lat     REAL NOT NULL,
    lon     REAL NOT NULL,
    rol     TEXT
);

CREATE TABLE IF NOT EXISTS climatology (
    point_id  TEXT NOT NULL,
    date      TEXT NOT NULL,
    precip_mm REAL,
    PRIMARY KEY (point_id, date)
);

CREATE TABLE IF NOT EXISTS thresholds (
    point_id          TEXT PRIMARY KEY,
    n_days            INTEGER,
    years             REAL,
    wet_days_per_year REAL,
    mean_annual_mm    REAL,
    p50_wet           REAL,
    p90_wet           REAL,
    p95_wet           REAL,
    p99_wet           REAL,
    p95_3d_wet        REAL,
    p99_3d_wet        REAL,
    p99_all           REAL,
    max_daily         REAL,
    source            TEXT,
    computed_at       TEXT
);

CREATE TABLE IF NOT EXISTS forecast_hourly (
    point_id  TEXT NOT NULL,
    run_date  TEXT NOT NULL,
    model     TEXT NOT NULL,
    ts        TEXT NOT NULL,
    precip_mm REAL,
    PRIMARY KEY (point_id, run_date, model, ts)
);

CREATE TABLE IF NOT EXISTS forecast_extra (
    point_id      TEXT NOT NULL,
    run_date      TEXT NOT NULL,
    ts            TEXT NOT NULL,
    rh            REAL,
    wind          REAL,
    cape          REAL,
    soil_moisture REAL,
    PRIMARY KEY (point_id, run_date, ts)
);

CREATE TABLE IF NOT EXISTS ensemble_daily (
    point_id  TEXT NOT NULL,
    run_date  TEXT NOT NULL,
    model     TEXT NOT NULL,
    date      TEXT NOT NULL,
    mean_mm   REAL,
    p10_mm    REAL,
    p50_mm    REAL,
    p90_mm    REAL,
    prob10    REAL,
    prob25    REAL,
    prob50    REAL,
    n_members INTEGER,
    PRIMARY KEY (point_id, run_date, model, date)
);

CREATE TABLE IF NOT EXISTS run_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS road_alerts (
    id             TEXT PRIMARY KEY,
    codigo         TEXT,
    motivo         TEXT,
    estado_via     TEXT,
    titulo         TEXT,
    descripcion    TEXT,
    situacion      TEXT,
    lat            REAL,
    lng            REAL,
    region         TEXT,
    provincia      TEXT,
    distrito       TEXT,
    via_codigo     TEXT,
    via_nombre     TEXT,
    km             TEXT,
    sentido        TEXT,
    fecha_evento   TEXT,
    vigente_desde  TEXT,
    vigente_hasta  TEXT,
    administrador  TEXT,
    fuente         TEXT,
    vencida        INTEGER,
    last_action    TEXT
);

CREATE TABLE IF NOT EXISTS nino_status (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    payload   TEXT,
    fetched   TEXT
);

CREATE TABLE IF NOT EXISTS rule_verification (
    umbral       REAL PRIMARY KEY,
    hits         INTEGER,
    misses       INTEGER,
    false_alarms INTEGER,
    n_points     INTEGER
);
"""

# Columnas agregadas despues de la primera version: se aplican sobre bases ya existentes.
MIGRATIONS = (
    "ALTER TABLE thresholds ADD COLUMN p95_3d_wet REAL",
    "ALTER TABLE thresholds ADD COLUMN p99_3d_wet REAL",
)


def _migrate(conn: sqlite3.Connection) -> None:
    for sql in MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # la columna ya existe


def ensure_dirs() -> None:
    for folder in (DATA_DIR, CACHE_DIR, OUT_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def _cache_path(url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / f"{digest}.json"


def http_json(
    url: str,
    *,
    retries: int = 4,
    timeout: int = 90,
    use_cache: bool = True,
    max_age_hours: float | None = None,
) -> dict:
    """Descarga y parsea JSON. Guarda en cache para no repetir llamadas."""
    ensure_dirs()
    path = _cache_path(url)

    if use_cache and path.exists():
        fresh = True
        if max_age_hours is not None:
            age_h = (time.time() - path.stat().st_mtime) / 3600.0
            fresh = age_h <= max_age_hours
        if fresh:
            return json.loads(path.read_text(encoding="utf-8"))

    last_err: Exception | None = None
    for attempt in range(retries):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"API devolvio error: {data.get('reason')}")
            if use_cache:
                path.write_bytes(raw)
            return data
        except urllib.error.HTTPError as exc:
            last_err = exc
            # 429 = limite de tasa: hay que esperar bastante, no reintentar rapido.
            time.sleep((18 * (attempt + 1)) if exc.code == 429 else 1.5 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"Fallo la descarga {url} :: {last_err}")


@contextmanager
def db():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def load_points() -> list[dict]:
    """Lee config/points.csv (fuente de verdad de la red de monitoreo)."""
    import csv

    with POINTS_CSV.open("r", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for row in rows:
        out.append(
            {
                "id": row["id"].strip(),
                "name": row["name"].strip(),
                "dept": row["dept"].strip(),
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "rol": row["rol"].strip(),
            }
        )
    return out


def sync_points(conn: sqlite3.Connection, points: list[dict]) -> None:
    conn.executemany(
        """INSERT INTO points (id, name, dept, lat, lon, rol)
           VALUES (:id, :name, :dept, :lat, :lon, :rol)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, dept=excluded.dept,
             lat=excluded.lat, lon=excluded.lon, rol=excluded.rol""",
        points,
    )


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO run_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM run_meta")}


def banner(text: str) -> None:
    print("\n" + "=" * 72)
    print(text)
    print("=" * 72)
