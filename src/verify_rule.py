"""Verificacion de la regla de disparo sobre el registro historico 1991-2020.

La pregunta que responde: si el umbral p95 de un punto se usa como alarma,
¿cuantos eventos severos detecta y cuantas falsas alarmas genera?

Se evalua el UMBRAL, no el pronostico. Es un analisis de prognosis perfecta:
asume que la lluvia observada es la que se habria pronosticado. Sirve para
decidir si el umbral esta bien puesto; no mide la habilidad del modelo.

Metricas por umbral de impacto T (10, 25, 50 mm/dia):
    POD = aciertos / (aciertos + fallos)      fraccion de eventos detectados
    FAR = falsas alarmas / (aciertos + FA)    fraccion de alarmas sin evento
    CSI = aciertos / (aciertos + fallos + FA) indice de amenaza critica

Uso:
    python verify_rule.py
"""

from __future__ import annotations

import sys
from collections import defaultdict

from common import OUT_DIR, banner, db

UMBRALES_IMPACTO = (10.0, 25.0, 50.0)


def matriz(valores: list[float], p95: float, impacto: float) -> tuple[int, int, int, int]:
    """Devuelve (aciertos, fallos, falsas alarmas, negativos correctos)."""
    a = b = c = d = 0
    for v in valores:
        alarma = v >= p95
        evento = v >= impacto
        if alarma and evento:
            a += 1
        elif evento:
            b += 1
        elif alarma:
            c += 1
        else:
            d += 1
    return a, b, c, d


def metricas(a: int, b: int, c: int) -> dict:
    pod = a / (a + b) if (a + b) else None
    far = c / (a + c) if (a + c) else None
    csi = a / (a + b + c) if (a + b + c) else None
    return {"pod": pod, "far": far, "csi": csi, "eventos": a + b, "alarmas": a + c}


def main() -> int:
    banner("Verificacion de la regla de disparo (prognosis perfecta)")

    with db() as conn:
        valores: dict[str, list[float]] = defaultdict(list)
        for r in conn.execute("SELECT point_id, precip_mm FROM climatology ORDER BY point_id, date"):
            valores[r["point_id"]].append(r["precip_mm"])
        p95 = {
            r["point_id"]: r["p95_wet"]
            for r in conn.execute("SELECT point_id, p95_wet FROM thresholds")
        }

    if not valores:
        print("  No hay climatologia cargada.")
        return 1

    acumulado = {t: [0, 0, 0, 0] for t in UMBRALES_IMPACTO}
    por_punto: dict[str, dict[float, tuple[int, int, int]]] = defaultdict(dict)

    for pid, serie in valores.items():
        umbral = p95.get(pid)
        if not umbral:
            continue
        for t in UMBRALES_IMPACTO:
            a, b, c, d = matriz(serie, umbral, t)
            fila = acumulado[t]
            fila[0] += a
            fila[1] += b
            fila[2] += c
            fila[3] += d
            por_punto[pid][t] = (a, b, c)

    lineas = [
        "# Verificación de la regla de disparo",
        "",
        "Evaluación del umbral p95 como alarma, sobre 1991–2020 en los 21 puntos.",
        "**Asume prognosis perfecta**: mide si el umbral está bien puesto, no la",
        "habilidad del pronóstico.",
        "",
        "| Evento (mm/día) | POD | FAR | CSI | Eventos | Alarmas |",
        "|---|---:|---:|---:|---:|---:|",
    ]

    with db() as conn:
        conn.execute("DELETE FROM rule_verification")
        print(f"\n  {'Umbral':>8} {'POD':>7} {'FAR':>7} {'CSI':>7} {'Eventos':>9} {'Alarmas':>9}")
        for t in UMBRALES_IMPACTO:
            a, b, c, _ = acumulado[t]
            m = metricas(a, b, c)
            conn.execute(
                "INSERT OR REPLACE INTO rule_verification "
                "(umbral, hits, misses, false_alarms, n_points) VALUES (?, ?, ?, ?, ?)",
                (t, a, b, c, len(por_punto)),
            )
            print(
                f"  {t:>6.0f} mm {m['pod']:>7.2f} {m['far']:>7.2f} {m['csi']:>7.2f} "
                f"{m['eventos']:>9} {m['alarmas']:>9}"
            )
            lineas.append(
                f"| {t:.0f} | {m['pod']:.2f} | {m['far']:.2f} | {m['csi']:.2f} | "
                f"{m['eventos']} | {m['alarmas']} |"
            )

    print("\n  Lectura:")
    a, b, c, _ = acumulado[25.0]
    m = metricas(a, b, c)
    if m["pod"] is not None:
        print(
            f"    Con el umbral p95, se detecta el {m['pod']*100:.0f}% de los dias con "
            f">=25 mm, y el {m['far']*100:.0f}% de las alarmas no tienen evento."
        )
        if m["pod"] < 0.5:
            print("    -> El umbral deja pasar mas de la mitad de los eventos severos.")
        if m["far"] > 0.5:
            print("    -> Y ademas dispara seguido sin evento: mal discriminador.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ruta = OUT_DIR / "verificacion_regla.md"
    ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    print(f"\n  -> {ruta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
