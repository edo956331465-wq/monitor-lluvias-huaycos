"""Ingesta de las alertas viales de SUTRAN y del estado de El Niño.

Ambas capas salen del mismo backend que alimenta el Mapa Interactivo de Alertas
(https://alertasviales.sutran.gob.pe). Se descubrieron leyendo su bundle, no son
endpoints documentados: si SUTRAN los cambia, esto se rompe y hay que volver a
inspeccionar el JavaScript del sitio.

    GET {API}/public/alerts   -> alertas viales activas (cierre, restricción, etc.)
    GET {API}/public/nino     -> estado oficial de El Niño Costero (SENAMHI/ENFEN)

Uso:
    python fetch_sutran.py            # usa cache de 30 minutos
    python fetch_sutran.py --refresh
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from common import banner, db, http_json, set_meta

API = "https://api.sigea.sutran.gob.pe"
MAX_AGE_H = 0.5  # las alertas cambian durante el día; media hora es razonable

INSERT_SQL = """INSERT OR REPLACE INTO road_alerts
   (id, codigo, motivo, estado_via, titulo, descripcion, situacion, lat, lng,
    region, provincia, distrito, via_codigo, via_nombre, km, sentido,
    fecha_evento, vigente_desde, vigente_hasta, administrador, fuente,
    vencida, last_action)
   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main() -> int:
    refresh = "--refresh" in sys.argv
    age = 0.0 if refresh else MAX_AGE_H
    banner("Alertas viales SUTRAN y estado de El Niño")

    try:
        payload = http_json(f"{API}/public/alerts", max_age_hours=age, retries=4)
    except Exception as exc:  # noqa: BLE001
        print(f"  Alertas: ERROR {exc}")
        return 1

    alerts = payload.get("data") or []
    with db() as conn:
        conn.execute("DELETE FROM road_alerts")
        for a in alerts:
            conn.execute(
                INSERT_SQL,
                (
                    a.get("id"),
                    a.get("codigo"),
                    a.get("motivo"),
                    a.get("estadoVia"),
                    a.get("titulo"),
                    a.get("descripcion"),
                    a.get("situacionActual"),
                    _num(a.get("lat")),
                    _num(a.get("lng")),
                    a.get("region"),
                    a.get("provincia"),
                    a.get("distrito"),
                    a.get("viaCodigo"),
                    a.get("viaNombre"),
                    a.get("km"),
                    a.get("sentido"),
                    a.get("fechaEvento"),
                    a.get("vigenteDesde"),
                    a.get("vigenteHasta"),
                    a.get("administrador"),
                    a.get("fuente"),
                    1 if a.get("vencida") else 0,
                    a.get("lastActionAt"),
                ),
            )
        set_meta(conn, "sutran_run_utc", datetime.now(timezone.utc).isoformat(timespec="seconds"))

    por_estado: dict[str, int] = {}
    por_motivo: dict[str, int] = {}
    for a in alerts:
        por_estado[a.get("estadoVia") or "?"] = por_estado.get(a.get("estadoVia") or "?", 0) + 1
        por_motivo[a.get("motivo") or "?"] = por_motivo.get(a.get("motivo") or "?", 0) + 1
    print(f"  Alertas activas: {len(alerts)}  {por_estado}")
    print(f"  Por motivo:      {por_motivo}")

    # El Niño: es un objeto chico, se guarda tal cual.
    try:
        nino = http_json(f"{API}/public/nino", max_age_hours=6, retries=3).get("data") or {}
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO nino_status (id, payload, fetched) VALUES (1, ?, ?)",
                (
                    json.dumps(nino, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
        alerta = nino.get("alerta") or {}
        print(f"  El Niño: {alerta.get('nombreEvento')} | magnitud {alerta.get('magnitud')} "
              f"({alerta.get('vigencia')})")
    except Exception as exc:  # noqa: BLE001
        print(f"  El Niño: ERROR {exc} (se conserva el valor anterior si existía)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
