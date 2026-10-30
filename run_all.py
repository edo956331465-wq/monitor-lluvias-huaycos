"""Orquestador del pipeline completo.

Uso:
    python run_all.py              # corrida normal (reutiliza cache y climatologia)
    python run_all.py --refresh    # fuerza redescarga de todo
    python run_all.py --verify     # ademas corre la validacion 2017/2023
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
PY = sys.executable

try:  # la consola de Windows abre en cp1252 y rompe con las rutas acentuadas
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def run(script: str, *args: str) -> bool:
    print(f"\n>>> {script} {' '.join(args)}")
    t0 = time.time()
    proc = subprocess.run(
        [PY, str(SRC / script), *args],
        cwd=str(SRC),
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    ok = proc.returncode == 0
    print(f"<<< {script}: {'ok' if ok else 'FALLO'} ({time.time() - t0:.1f}s)")
    return ok


def main() -> int:
    refresh = "--refresh" in sys.argv
    verify = "--verify" in sys.argv
    args = ("--refresh",) if refresh else ()
    t0 = time.time()

    if not run("fetch_geodata.py", *args):
        print("  (se continua sin geodatos: el tablero omite los mapas)")
    if not run("fetch_sutran.py", *args):
        print("  (se continua sin alertas viales)")
    if not run("ingest_climatology.py", *args):
        return 1
    if not run("ingest_forecast.py", *args):
        return 1
    if not run("verify_rule.py"):
        print("  (se continua sin verificacion de la regla)")
    gold_sisap = HERE / "data" / "gold" / "sisap_dashboard.json"
    if gold_sisap.exists():
        print("\n>>> SISAP: usando contrato Gold publicado por la arquitectura medallón")
    elif not run("fetch_sisap.py"):
        print("  (se continua sin precios SISAP)")
    if not run("fetch_ports_marine.py"):
        print("  (se continúa con el último contrato válido de puertos y mar)")
    if not run("export_fish_supply.py"):
        print("  (se continúa con el último contrato Gold de pescados)")
    if not run("fetch_fish_provider_prices.py"):
        print("  (se continúa sin precios observados del proveedor)")
    if not run("fetch_fisheries_governance.py"):
        print("  (se continúa con el último contrato de gobierno pesquero)")
    if not run("build_prices.py"):
        print("  (la hoja de precios SISAP no se genero)")
    if not run("build_dashboard.py"):
        return 1
    if not run("build_supply.py"):
        print("  (la hoja de riesgo de abastecimiento no se generó)")
    if not run("build_ports.py"):
        print("  (la hoja de puertos y condiciones marinas no se generó)")
    if not run("build_fish.py"):
        print("  (la hoja de pescados y riesgo portuario no se generó)")
    if not run("publish_gas.py"):
        print("  (el paquete de Apps Script no se genero)")
    if verify:
        run("verify_events.py")

    print(f"\nPipeline completo en {time.time() - t0:.1f}s")
    print(f"  Tablero: {HERE / 'out' / 'dashboard.html'}")
    print(f"  Apps Script: {HERE / 'out' / 'apps_script'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
