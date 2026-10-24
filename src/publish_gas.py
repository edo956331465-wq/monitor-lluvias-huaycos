"""Empaqueta el tablero para publicarlo como Web App de Google Apps Script.

Apps Script no puede ejecutar Python ni numpy, asi que no puede calcular nada:
solo sirve el HTML. El calculo sigue ocurriendo donde corre este pipeline.

Genera en out/apps_script/:
    Code.gs           punto de entrada (doGet)
    Dashboard.html    el tablero completo, autocontenido
    appsscript.json   manifiesto con zona horaria y acceso publico
    LEEME.md          instrucciones de despliegue

Uso:
    python publish_gas.py
"""

from __future__ import annotations

import json
import shutil

from common import OUT_DIR, banner, get_meta, db

TARGET = OUT_DIR / "apps_script"

CODE_GS = """/**
 * Monitor de lluvia y riesgo de huaicos · Costa peruana
 *
 * Web App que sirve el tablero ya calculado por el pipeline en Python.
 * Apps Script no hace ningun calculo: solo entrega el HTML.
 *
 * Para actualizar los datos: vuelve a correr el pipeline y reemplaza
 * Dashboard.html por el archivo nuevo.
 */

function doGet() {
  return HtmlService.createHtmlOutputFromFile('Dashboard')
    .setTitle('Monitor de lluvia · Costa peruana')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

/**
 * Diagnostico rapido: ejecuta esta funcion desde el editor para comprobar
 * que el proyecto esta bien cargado. Devuelve el tamano del tablero.
 */
function verificar() {
  const html = HtmlService.createHtmlOutputFromFile('Dashboard').getContent();
  const generado = (html.match(/Generado (\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2})/) || [])[1] || 'desconocido';
  Logger.log('Tablero cargado: %s caracteres, generado %s', html.length, generado);
}
"""

MANIFEST = {
    "timeZone": "America/Lima",
    "exceptionLogging": "STACKDRIVER",
    "runtimeVersion": "V8",
    "webapp": {
        "access": "ANYONE_ANONYMOUS",
        "executeAs": "USER_DEPLOYING",
    },
}

LEEME = """# Publicar el tablero como Web App de Google

## Lo primero: qué hace y qué no

Apps Script **no puede ejecutar el pipeline**. No hay Python, no hay numpy y no
hay base de datos de 17 MB. Lo que sí puede hacer, y hace bien, es **servir el
tablero ya calculado** en una URL de Google, con control de acceso.

Es decir: el cálculo sigue pasando en la máquina donde corres `run.ps1`.
Este paquete solo publica el resultado.

## Opción A · copiar y pegar (5 minutos, sin instalar nada)

1. Entra a <https://script.google.com> y crea un proyecto nuevo.
2. Renómbralo, por ejemplo `Monitor lluvias Perú`.
3. Abre `Code.gs` y reemplaza todo su contenido por el de `Code.gs` de esta carpeta.
4. Menú **Archivo → Nuevo → Archivo HTML**, ponle exactamente el nombre
   `Dashboard` (sin `.html`, la extensión la pone Apps Script).
5. Reemplaza el contenido por el de `Dashboard.html` de esta carpeta.
6. Menú **Ver → Mostrar archivo de manifiesto** (si no está visible), y
   reemplaza `appsscript.json` por el de esta carpeta.
7. **Implementar → Nueva implementación → Aplicación web**.
   - Ejecutar como: *Yo*
   - Quién tiene acceso: *Cualquier persona* (o *Cualquier persona con cuenta de Google*)
8. Copia la URL que termina en `/exec`. Esa es tu tablero.

## Opción B · clasp (para no volver a pegar a mano)

```bash
npm install -g @google/clasp
clasp login
clasp create --type webapp --title "Monitor lluvias Peru" --root ./out/apps_script
clasp push
clasp deploy --description "primera version"
```

Con `clasp push` actualizas en un comando cada vez que el pipeline regenera
`Dashboard.html`.

## Actualizar los datos

El tablero es una foto del momento en que se generó (mira la fecha en el
encabezado). Para refrescarlo:

```powershell
.\\run.ps1                 # recalcula
python src\\publish_gas.py  # rearma esta carpeta
```

y vuelve a subir `Dashboard.html`. Si usas `clasp`: `clasp push` y listo.

## Si quieres que se actualice solo

Apps Script puede correr con un activador por tiempo, pero **no puede ejecutar
Python**. Las opciones reales son:

- **GitHub Actions** con un cron que corra el pipeline y publique en GitHub
  Pages. Es la vía más simple y gratis, y no depende de Google.
- Una **máquina o servidor** propio que corra `run.ps1` a diario y suba el HTML.
- Reimplementar la ingesta en Apps Script con `UrlFetchApp` (posible para el
  pronóstico, muy incómodo para los 30 años de climatología).

## Alternativa sin Apps Script

Si lo único que necesitas es compartir el tablero, el archivo
`out/dashboard.html` es autocontenido: se puede subir tal cual a GitHub Pages,
Cloudflare Pages, Netlify o cualquier hosting estático, y se abre sin servidor.
Es menos "Google", pero bastante más simple.
"""


def main() -> int:
    banner("Empaquetando Web App de Google Apps Script")

    src_html = OUT_DIR / "dashboard.html"
    if not src_html.exists():
        print("  Falta out/dashboard.html. Corre build_dashboard.py primero.")
        return 1

    TARGET.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_html, TARGET / "Dashboard.html")
    (TARGET / "Code.gs").write_text(CODE_GS, encoding="utf-8")
    (TARGET / "appsscript.json").write_text(
        json.dumps(MANIFEST, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (TARGET / "LEEME.md").write_text(LEEME, encoding="utf-8")

    with db() as conn:
        meta = get_meta(conn)
    corrida = meta.get("forecast_run_date", "?")

    size_kb = (TARGET / "Dashboard.html").stat().st_size / 1024
    print(f"  Corrida incluida: {corrida}")
    print(f"  Dashboard.html: {size_kb:.0f} KB (autocontenido, sin recursos externos)")
    print(f"  -> {TARGET}")
    print("  Siguiente paso: abrir LEEME.md y seguir la opcion A.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
