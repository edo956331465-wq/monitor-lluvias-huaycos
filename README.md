# Monitor de lluvia y riesgo de huaicos · Costa peruana

Prototipo funcional de vigilancia de precipitación para las 18 ciudades costeras
del Perú y sus 3 cuencas altas aportantes. Ingiere pronóstico multimodelo y
ensemble, lo compara contra la climatología 1991–2020 de cada punto y genera un
tablero HTML autocontenido con mapa nacional de alertas.

**Estado:** fase 1 completa (ingesta + línea base + tablero + mapas + validación
histórica). No es un modelo entrenado todavía, y no debe usarse para decisiones
de emergencia sin validación oficial.

---

## Qué produce

| Archivo | Contenido |
|---|---|
| `out/dashboard.html` | Tablero operativo. Se abre con doble clic, no necesita servidor. |
| `out/dashboard_preview.png` | Captura de la vista principal (referencia, no se regenera sola). |
| `out/pronostico_resumen.csv` | Tabla plana por punto, lista para Excel o Power BI. |
| `out/dashboard_data.json` | Los mismos datos en JSON, para otra app. |
| `out/validacion_2017_2023.md` | Validación de la línea base contra los eventos 2017 y 2023. |
| `out/apps_script/` | Paquete listo para publicar el tablero como Web App de Google. |
| `data/warehouse.sqlite` | Almacén con climatología, pronóstico y umbrales. |
| `data/geodata/` | Contorno del Perú, regiones, ríos y quebradas (simplificados). |
| `data/gold/sisap_dashboard.json` | Contrato Gold versionado con precios y procedencias para GitHub Pages. |

El tablero abre con el **mapa nacional de alertas** y deja el resto del detalle en
secciones plegables: transecto costero, cuencas altas, mapas de quebradas por
zona, pronóstico diario a 16 días, tabla por punto y metodología.

## Cómo ejecutarlo

Requiere Python 3.11+ y `numpy`. Nada más: no hay `requests`, ni `pandas`, ni
`geopandas`, ni claves de API.

```powershell
.\run.ps1                 # corrida normal (usa caché)
.\run.ps1 --refresh       # fuerza redescarga de todo
.\run.ps1 --verify        # además corre la validación 2017/2023
```

O directamente:

```powershell
python run_all.py --verify
```

La primera corrida tarda 3–5 minutos (descarga ~21 años-punto de climatología y
la geometría de OSM). Las siguientes son de segundos, porque todo queda en
`data/cache/` y `data/geodata/`.

## Integración SISAP con arquitectura medallón

El dashboard no consulta SISAP desde GitHub Actions. La adquisición y calidad se
resuelven localmente en el proyecto hermano `Precios Mayoristas`:

```text
SISAP → Bronze (HTML + metadata/hash) → Silver (Parquet limpio y deduplicado)
      → Gold del dashboard (JSON ligero) → GitHub → GitHub Pages
```

`src/export_sisap_medallion.py` lee la partición Silver más reciente, conserva
la última fecha válida por mercado y publica el contrato
`data/gold/sisap_dashboard.json`. Para exportar, versionar y subirlo:

```powershell
.\scripts\sync_sisap_medallion.ps1
```

La actualización de GitHub Pages se activa con ese `push`; si SISAP falla, se
mantiene el último contrato Gold válido y el sitio no queda vacío.

## Estructura

```
config/points.csv          red de monitoreo (lat, lon, rol)
src/common.py              HTTP con caché, reintentos y limitador de tasa; esquema SQLite
src/fetch_geodata.py       contorno (geoBoundaries) + ríos y quebradas (OSM) + red vial
src/fetch_sutran.py        alertas viales de SUTRAN y estado oficial de El Niño
src/ingest_climatology.py  ERA5 1991–2020 → umbrales p95/p99 de 1 y 3 días
src/ingest_forecast.py     ECMWF/GFS/ICON + ensemble de 50 miembros
src/build_dashboard.py     índice de riesgo + mapa nacional + tablero HTML
src/export_sisap_medallion.py  Silver SISAP → contrato Gold del dashboard
src/verify_events.py       validación contra 2017 y 2023
src/publish_gas.py         empaqueta el tablero para Google Apps Script
run_all.py                 orquestador
scripts/sync_sisap_medallion.ps1  publicación local diaria de SISAP Gold
```

Para agregar una ciudad, basta una fila en `config/points.csv` y volver a correr.
No hay nada hardcodeado en el código.

## Fuentes de datos

Todas gratuitas y sin clave. La elección no es casual:

| Fuente | Para qué | Por qué esta |
|---|---|---|
| **Open-Meteo** (forecast + ensemble) | Pronóstico operativo | Agrega ECMWF IFS, GFS e ICON en una sola llamada JSON |
| **ECMWF IFS 0.25°** | Modelo de referencia | El de mejor desempeño en Sudamérica; ensemble de 50 miembros |
| **NOAA GFS / DWD ICON** | Contraste multimodelo | La dispersión entre modelos es información, no ruido |
| **ERA5** (Open-Meteo Archive) | Climatología 1991–2020 | 30 años homogéneos, sin huecos de estación |
| **OpenStreetMap** (Overpass) | Ríos y quebradas | Única fuente abierta con nombres locales y trazado real |
| **geoBoundaries** | Contorno y regiones del Perú | Límites abiertos, simplificados y versionados |
| **SUTRAN** (SIGEA) | Alertas viales, red vial nacional y departamental | Fuente oficial del estado de las carreteras |
| **SENAMHI/ENFEN** vía SUTRAN | Estado de El Niño Costero | El dato oficial, ya consolidado en un JSON |

### Sobre la fuente que planteaste al inicio

El endpoint `weather.com/docs/openapi/weather-alerts-headlines-3-0` devuelve
**titulares de alertas** de NWS, Environment Canada, MeteoAlarm, ABOM y KMA.
Su cobertura declarada es EE.UU., Canadá, Japón, Europa Occidental, Australia y
Corea del Sur: **Perú no está**. Y aunque estuviera, son textos, no milímetros.

Sirve como capa de contexto si algún día el proyecto se internacionaliza. No sirve
como insumo de un modelo de lluvia.

### Fuentes que faltan y hay que agregar en fase 2

| Fuente | Aporta | Acceso |
|---|---|---|
| **SENAMHI** (estaciones) | Verdad terreno para calibrar | Web + WIS2/WMO |
| **GPM IMERG** | Lluvia satelital cada 30 min, 0.1° | NASA GES DISC (login gratuito) |
| **CHIRPS** | Lluvia diaria 1981–hoy, 0.05° | HTTP directo |
| **ENFEN** | Estado oficial de El Niño Costero | Web / PDF |
| **INGEMMET** | Susceptibilidad a huaicos por quebrada | WMS / descarga |
| **ANA** | Caudales y niveles de río, límites de cuenca | Web |
| **CENEPRED / INEI** | Exposición de población y vivienda | Descarga |

## Metodología

### 1. Umbrales climatológicos

Por punto se calculan dos familias, sobre 1991–2020:

- **1 día**: p90/p95/p99 de los días con ≥1 mm, más el máximo del registro.
- **3 días**: los mismos percentiles sobre la suma móvil de 3 días.

La segunda familia existe porque en la costa peruana el disparador no es solo el
aguacero: tres días de lluvia moderada saturan el suelo y activan una quebrada.
Se exige un mínimo de 20 muestras para publicar un percentil; si no, se usa un
piso absoluto (10 mm/día, 20 mm/3 días).

### 2. Índice de riesgo (v0, heurístico)

```
R = max( 24h / max(p95_1d, 10),  72h / max(p95_3d, 20) )
V = min(R / 2, 1)
I = min(máx_mm_h / 20, 1)
P = P(≥25 mm en 24 h)  del ensemble ECMWF (50 miembros)

score = 0.40·V + 0.35·I + 0.25·P
```

Cortes: **Bajo** < 0,20 · **Medio** 0,20–0,38 · **Alto** 0,38–0,55 · **Muy alto** ≥ 0,55.

Estar justo en el p95 equivale a `V = 0,5`: ya es un día notable, no un día normal.
Duplicar ese umbral satura la componente. Es deliberadamente transparente y **no
es un modelo entrenado**: su función es ordenar la vigilancia y establecer la
línea base contra la que se medirá el clasificador de la fase 3.

### 3. Los mapas

- **Mapa nacional**: contorno y regiones del Perú (geoBoundaries) con los 21
  puntos coloreados por nivel. Los tres con anillo punteado son las zonas que
  tienen mapa de quebradas en detalle. Es interactivo: dos botones encienden y
  apagan la capa de cuencas y la de alertas viales por separado, y una leyenda
  bajo el mapa explica cada símbolo.
- **Zoom y detalle progresivo**: rueda del ratón o botones para acercar hasta 12×,
  arrastre para moverse. Al pasar de 2× aparece la **red vial departamental**, que
  a escala nacional sería una maraña. Los marcadores y las etiquetas se
  reescalan de forma sublineal (z^0,6) para seguir legibles sin tapar el mapa, y
  las etiquetas que quedarían cortadas por el encuadre se ocultan.
- **Mapas de zona**: ríos y quebradas con trazado real de OpenStreetMap, filtrado
  para quitar acequias y vías mal etiquetadas. Cada cauce toma el color del punto
  de monitoreo más cercano.

El color del cauce **no** es una predicción por tramo: con celdas de 0,25° todas
las quebradas de una misma zona comparten señal. El mapa sirve para saber *dónde*
mirar, no para distinguir quebradas vecinas.

Los dos tipos de marcador son deliberadamente distintos: **círculo** para el punto
de monitoreo de cuenca (el color es el nivel), **rombo** para la alerta vial de
SUTRAN (el color es el estado de la vía). Mezclarlos con el mismo símbolo haría
ilegible el mapa justo cuando más se necesita.

El tamaño también codifica información: el círculo crece con la lluvia prevista a
72 h (escala de raíz cuadrada, para que un evento de 70 mm no aplaste a uno de 20)
y el rombo crece con la severidad del corte. La leyenda lista **cada variante**,
no un ejemplar genérico, y explica qué significa el tamaño.

La interactividad es JavaScript plano embebido, sin librerías ni CDN, para que
siga funcionando dentro del iframe de Google Apps Script.

### 4. Alertas viales y red vial

El mapa nacional superpone la **red vial nacional** de SUTRAN y las **alertas viales
activas** como rombos: rojo interrumpido, naranja restringido, verde normal. Debajo
del mapa hay una sección plegable con la tabla completa, ordenada por estado de la
vía —interrumpidas, restringidas, normales— y dentro de cada grupo los fenómenos
naturales primero.

Esto cierra el circuito del proyecto: el pronóstico dice *dónde puede llover*, y
SUTRAN dice *qué vía ya está cortada*. Un huaico que corta la Carretera Central
aparece en las dos capas.

Los datos salen de `api.sigea.sutran.gob.pe`, el mismo backend del
[Mapa Interactivo de Alertas](https://alertasviales.sutran.gob.pe/). **No son
endpoints documentados**: se descubrieron leyendo el bundle del sitio. Si SUTRAN
los cambia, la ingesta falla y hay que volver a inspeccionar su JavaScript.

### 5. Contexto El Niño

El mismo backend publica el estado oficial de El Niño Costero (fuente SENAMHI/ENFEN),
que se muestra como una franja bajo la lectura del día. Es el dato que le da sentido
a todo lo demás: sin él, un tablero de lluvia en octubre no se distingue de uno de
enero.

### 6. Por qué cuencas altas

En la costa peruana casi no llueve. El peligro real es que llueva fuerte en la
cuenca alta y el agua baje por una quebrada como huaico o aluvión —Chosica 2017,
Yaku 2023—. Por eso la red incluye Huancabamba, Santa Eulalia y Yauyos.

## Publicar en Google

`out/apps_script/` trae todo lo necesario para servir el tablero como Web App de
Google Apps Script (`Code.gs`, `Dashboard.html`, `appsscript.json` y `LEEME.md`).

**Apps Script sirve el HTML, no calcula nada.** No puede: no hay Python, ni numpy,
ni una base de 17 MB. El cálculo sigue ocurriendo donde corres `run.ps1`; Google
solo publica el resultado en una URL con control de acceso.

Si el objetivo es únicamente compartir el tablero, `out/dashboard.html` es
autocontenido y se puede subir tal cual a GitHub Pages, Cloudflare Pages o
cualquier hosting estático. Es menos "Google", pero más simple y sin cuotas.

## Validación

`verify_events.py` compara la lluvia de ERA5 durante 2017 y 2023 contra la
climatología del mismo punto. Resultado de la corrida actual:

| Evento | Puntos sobre su p99 | Pico más alto |
|---|---|---|
| 2017 · El Niño Costero | 12 de 21 | Piura, 130,6 mm (TR ≈ 10 años) |
| 2023 · Yaku | 10 de 21 | Chiclayo, 106,2 mm (sin precedente en 30 años) |

Los máximos coinciden con los impactos documentados, lo que confirma que la
cadena de datos captura la señal donde importa. **Pero esto no es una
verificación de pronóstico**: para eso hacen falta archivos de pronóstico
histórico, que no son gratuitos.

## Limitaciones conocidas

- **Resolución efectiva.** Los 21 puntos caen en solo **20 celdas** del retículo
  de 0,25° (~28 km). Chosica y Santa Eulalia comparten celda y por eso muestran
  valores idénticos: no es un error, es el límite del modelo.
- **Subestimación orográfica.** La costa es una franja de 50–150 km; un modelo
  global no resuelve las quebradas. La lluvia real en ladera suele ser mayor.
- **ERA5 suaviza extremos convectivos.** Los umbrales sirven para ordenar, no
  como valor absoluto de diseño hidráulico.
- **Cobertura de OSM es desigual.** Algunas quebradas con nombre en los reportes
  oficiales no están mapeadas, y aparecen vías mal etiquetadas como cauces. El
  filtro descarta lo evidente, no lo hace perfecto.
- **Sin verdad terreno todavía.** La calibración contra estaciones SENAMHI y
  GPM IMERG es la fase 2, y es la que más va a mover los números.
- **Sin límites de cuenca.** Delimitar cuencas requiere el shapefile de ANA o
  INGEMMET; hoy el mapa muestra cauces, no polígonos de cuenca.
- **SUTRAN no documenta su API.** Los endpoints se obtuvieron leyendo su bundle;
  pueden cambiar sin aviso. La ingesta está aislada para que una caída no tumbe
  el resto del tablero.
- **Las alertas viales son del momento.** Se refrescan cada 30 minutos; el
  tablero es una foto, no un flujo en vivo.

## Hoja de ruta

**Fase 2 · Calibración (2–3 semanas).** ~~Verificación POD/FAR/CSI por umbral~~ ✔
hecha sobre la línea base (erify_rule.py). Falta lo que requiere datos externos:
ingesta de estaciones SENAMHI y GPM IMERG, y corrección de sesgo del pronóstico por
punto. Ese paso necesita credenciales de Earthdata o trabajo de scraping, así que
queda bloqueado hasta decidir la fuente.

**Fase 3 · Modelo (3–4 semanas).** Clasificador LightGBM por punto: target
`≥ X mm/24 h`. Features: precipitación NWP acumulada y máxima horaria, humedad
relativa, viento, CAPE, humedad del suelo, Niño 1+2, día del año, elevación.
Validación temporal estricta (entrenar ≤2022, probar 2023).

**Fase 4 · Impacto.** Cruce con susceptibilidad de INGEMMET y exposición de
CENEPRED para pasar de "va a llover" a "qué quebrada y a quién".

**Fase 5 · Operación.** Programador de tareas diario, alertas por correo o
Telegram cuando `score ≥ 0,38` en un punto costero.

## Licencias y atribución

- Open-Meteo: CC BY 4.0 — atribución obligatoria, uso no comercial gratuito.
- ERA5 / Copernicus C3S: licencia Copernicus, atribución requerida.
- OpenStreetMap: ODbL — atribución requerida, compartir igual.
- SUTRAN / SIGEA: datos del Estado peruano publicados para consulta pública;
  se citan con enlace a la fuente. No hay licencia explícita de reutilización
  comercial: verifica antes de redistribuir.
- geoBoundaries: CC BY 4.0.
- Los datos de SENAMHI, IMERG y CHIRPS tienen sus propios términos; revísalos
  antes de redistribuir.

El proyecto **no** usa la API de The Weather Company, que es de pago y con
términos restrictivos.

## Advertencia

Prototipo de investigación. La ausencia de alerta en este tablero **no** significa
ausencia de peligro. La fuente oficial de avisos en el Perú es el
[SENAMHI](https://www.senamhi.gob.pe/), y el estado del fenómeno El Niño lo
publica [ENFEN](https://enfen.imarpe.gob.pe/).
