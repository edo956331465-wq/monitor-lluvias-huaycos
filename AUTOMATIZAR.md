# Cómo dejar el tablero actualizándose solo

## Respuesta corta

**Hoy nada se actualiza solo.** El tablero es una foto del momento en que corriste
`run.ps1`. Si no lo corres, mañana muestra los datos de hoy.

Y no es que los datos "lleguen" al tablero: alguien tiene que ir a buscarlos. Eso es
lo que hace el pipeline.

| Fuente | ¿Cambia sola? | Quién la trae |
|---|---|---|
| Pronóstico del tiempo | Sí, cada 6 h | El pipeline, en cada corrida |
| Alertas viales de SUTRAN | Sí, durante el día | El pipeline, en cada corrida |
| Estado de El Niño (ENFEN) | Cada quincena | El pipeline, en cada corrida |
| Climatología 1991–2020 | **No**, es fija | Ya está calculada |
| Red vial, contornos, quebradas | Casi nunca | Ya están en `data/geodata` |

O sea: solo importa refrescar las tres primeras, y para eso basta con correr el
pipeline cada pocas horas — que es exactamente lo que automatiza esta guía.

## La forma recomendada: GitHub Actions

GitHub te presta una máquina virtual, corre el pipeline con un reloj, y publica el
tablero en una URL fija. **Gratis, y tu PC puede estar apagada.**

Probado en limpio: una máquina sin caché ni base de datos completa el tablero en
**4,3 minutos**. Con caché bajó a ~1 minuto.

### Lo que ya está listo en el proyecto

- `.github/workflows/tablero.yml` — corre cada 3 horas y publica en GitHub Pages.
- `.gitignore` — excluye caché y base de datos para no versionar basura.

### Los 5 pasos que faltan (los haces tú, una sola vez)

**1. GitHub.** Crea cuenta en <https://github.com> si no tienes.

**2. Crea un repositorio** vacío, por ejemplo `monitor-lluvias`. Público o privado:
- Público → minutos de Actions ilimitados.
- Privado → 2 000 min/mes gratis. Con caché, este proyecto usa ~250 al mes.

**3. Sube el proyecto.** La vía más simple sin instalar nada:

   - En la página del repo: **Add file → Upload files**
   - Arrastra **todo** menos `data/cache`, `data/warehouse.sqlite` y `out/`
   - Ojo: el explorador de Windows no muestra `.github` por defecto; activa
     "Elementos ocultos" o arrastra la carpeta completa.

   Con Git instalado sería:

   ```bash
   git init
   git add .
   git commit -m "monitor de lluvias"
   git branch -M main
   git remote add origin https://github.com/TU-USUARIO/monitor-lluvias.git
   git push -u origin main
   ```

**4. Activa Pages.** Settings → Pages → Source: **GitHub Actions**.

**5. Listo.** Entra a la pestaña **Actions** y pulsa *Run workflow* para la primera
corrida. En ~5 minutos tendrás la URL:

```
https://TU-USUARIO.github.io/monitor-lluvias/
```

Esa URL se actualiza sola cada 3 horas, para siempre, sin tu PC.

## Qué pasa con Google Apps Script

**Con GitHub Pages ya no lo necesitas.** Apps Script existía para servir el HTML en
una URL de Google; Pages hace lo mismo, gratis y sin cuotas ni el paso de "nueva
versión".

Si tu organización exige que sea Google, hay dos caminos:

1. **Mantener Apps Script y actualizarlo a mano** cada vez (lo que haces hoy).
2. **Automatizar también el push a Apps Script** con `clasp` dentro del workflow,
   usando un token de Google guardado como secreto del repo. Es factible, pero
   agrega un punto de falla y credenciales que hay que rotar. Solo si es requisito.

## Alternativa sin GitHub: Programador de tareas de Windows

Si prefieres no depender de Internet para el cómputo (aunque igual lo necesitas
para los datos):

```powershell
$accion = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"C:\ruta\al\proyecto\run.ps1`""
$disparo = New-ScheduledTaskTrigger -Daily -At 6am
Register-ScheduledTask -TaskName "Monitor lluvias" -Action $accion -Trigger $disparo
```

Contra: tu PC tiene que estar encendida a las 6 a.m., y el tablero solo se actualiza
en tu disco — para publicarlo seguirías necesitando subirlo a mano.

## Cuánto cuesta

| Pieza | Costo |
|---|---|
| GitHub Actions (repo público) | Gratis, ilimitado |
| GitHub Actions (repo privado) | Gratis hasta 2 000 min/mes; este proyecto usa ~250 |
| GitHub Pages | Gratis |
| Open-Meteo, ERA5, SUTRAN, OSM | Gratis, sin claves |

Costo real: **cero**, mientras el repo sea público o el uso quede bajo el límite.

## Si algo falla

El workflow no rompe el sitio: si un paso falla, GitHub Pages sigue sirviendo la
última versión buena. Revisa la pestaña **Actions** para ver el log.

Los puntos más frágiles, en orden:

1. **Overpass** (geometría de quebradas) — se cae con frecuencia y tiene límite de
   tasa. Por eso `data/geodata` se versiona: si falla, se usa lo último descargado.
2. **SUTRAN** — sus endpoints no están documentados y pueden cambiar sin aviso.
   La ingesta está aislada: si falla, el tablero sale sin alertas viales.
3. **Open-Meteo** — el más estable de los tres.
