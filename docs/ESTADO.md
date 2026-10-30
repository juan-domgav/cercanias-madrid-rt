# Estado del proyecto y traspaso

Documento de continuidad. Se escribe para que otra sesión pueda seguir sin
reconstruir el contexto. **Última actualización: 2026-10-29.**

## Modo de trabajo

- **Claude: arquitecto.** Decide diseño, revisa, verifica contra datos reales,
  escribe ADRs y decide qué se despliega.
- **codex y grok: implementadores.** Reciben encargos concretos y escriben el
  código. Nunca hacen commit; el arquitecto revisa el diff antes de integrar.
- Briefs previos, útiles como plantilla, en `/scratchpad/brief-*.md` de la sesión.
  Lo que funciona: encargo concreto, restricciones explícitas (`make ci` verde,
  nada de `# noqa`, usar `make` y no `uv run`), y decirles las trampas conocidas.

## Qué es esto

Información en tiempo real de Cercanías Madrid. Un tablón de salidas por
estación, con andén, destino, retraso y avisos, servido como fichero estático
tras CDN, más un bot de Telegram.

**En producción:**

| Pieza | Dónde |
|---|---|
| Web | https://storage.googleapis.com/cercanias-madrid-rt-web/index.html |
| Bot | `@Cercanias_Madrid_RT_Bot` · canal `@cercanias_madrid_rt` |
| API | `https://cercanias-api-549278575802.europe-southwest1.run.app` |
| Ingesta | Cloud Run job `cercanias-ingesta`, cada minuto |
| Horarios | Cloud Run job `cercanias-horarios`, cada noche a las 05:17 (Madrid) |
| Proyecto | `cercanias-en-hora` (GCP) · buckets `…-web` (público) y `…-privado` |

Detalle operativo en `docs/DESPLIEGUE.md`.

## Los hechos que gobiernan el diseño

Todo esto está **medido**, no supuesto. Cada uno costó una avería o una revisión.

1. **El prefijo del `tripId` rota a diario** (ADR-012). `1010V`, `1011S`, `1012D`…
   Lo estable son los **dos primeros dígitos** (`10` = Madrid). Filtrar por los
   cuatro deja la app sin trenes y **no falla**: filtra a cero.
2. **El núcleo desaparece del feed** en ~5 % de los sondeos (ADR-011), en los dos
   feeds a la vez, sin error. De ahí el «último bueno conocido».
3. **El GTFS viene rellenado a ancho fijo y de forma inconsistente**: `route_id`
   con relleno en `routes.txt` y sin él en `trips.txt`, y las **cabeceras**
   también. Sin limpiar claves y valores, los joins devuelven cero filas en
   silencio. Ha mordido tres veces.
4. **`trip_headsign` está vacío.** El destino se deriva de la última parada.
5. **El feed publica `CANCELED` y `SKIPPED`.** Durante semanas se afirmó lo
   contrario y se construyeron rodeos; el dato estaba en el fixture del 31/07.
   `SKIPPED` es la señal de un tren que se queda corto.
6. **Un `delay` de 0 puede ser un marcador**, no una lectura: el feed lo publica
   para viajes que aún no han salido. Nunca decir «en hora» sólo por eso.
7. **`block_id` no encadena servicios**: 517 de 39.108, y entre líneas distintas.
   El enlace de cabecera se deriva de la geometría del horario.
8. **macOS + uv + CPython 3.13**: los `.pth` editables quedan ocultos y `site.py`
   los omite en silencio (ADR-003). **Usar siempre `make`, nunca `uv run` suelto.**

## Decisiones de producto que no son obvias

- **La posición del usuario no sale del dispositivo.** El catálogo son unos KB;
  la distancia se calcula en el navegador. Evita tratar dato personal y mantiene
  la lectura como fichero estático (C-7).
- **La página pinta, no calcula.** Los tablones los precalcula la ingesta en
  Python probado y los publica en `boards.json`. Hubo una copia en JavaScript sin
  tests y las correcciones sólo llegaban a una de las dos.
- **Nunca inventar andén.** Sólo existe en el feed en vivo (A.6). Si el tren no
  está localizado, se dice «Andén —».
- **Avisar sólo cuando cambia una decisión.** Umbrales relativos a la maniobra de
  cada enlace, no a una mediana de población. Un aviso que no aporta enseña a
  ignorar los que sí.

## Despliegue actual (2026-10-29, RB-015 / PR #18)

Con el OK de Juan (29/10): PR #18 («fix(health): delay_coverage excluye viajes
suprimidos (RB-015)», commit `3009d3c`) mergeado como `8964b0c` (18:21:50
Madrid), con merge commit como los PR anteriores. `cercanias-ingesta`
reconstruido desde un `git archive` limpio de `8964b0c` con
`gcloud run jobs deploy cercanias-ingesta --source . --region europe-southwest1`,
**sin** banderas de configuración (listo a las 18:24:17 Madrid, generación 20).
Diff del `describe` antes/después: sólo la imagen y metadatos
(generación, `resourceVersion`, `lastUpdatedTime`, operación); env idéntico
(`ARCHIVAR_CANCELACIONES=1` y `ARCHIVAR_AVISOS=1` siguen), secreto de Telegram,
SA, recursos y Scheduler iguales. API, web, `cercanias-horarios` y
`cercanias-telegram-ops` sin cambios (siguen con su imagen anterior).

| Pieza | Ahora | Rollback |
|---|---|---|
| Job `cercanias-ingesta` | `cercanias-ingesta@sha256:e5ae9de9…ed10a781` (PR #18, `8964b0c`), `ARCHIVAR_CANCELACIONES=1`, `ARCHIVAR_AVISOS=1` | `gcloud run jobs update cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora --image europe-southwest1-docker.pkg.dev/cercanias-en-hora/cloud-run-source-deploy/cercanias-ingesta@sha256:b747dd76934340ef44373aa29c8f79d361115aee8ce0105de7156adabdebc9e8` (PR #15, `374c116`) |

Ingesta: `europe-southwest1-docker.pkg.dev/cercanias-en-hora/cloud-run-source-deploy/cercanias-ingesta@sha256:e5ae9de9fb0ea7efd9a3e1232cde65a19d33002c214e62a7cafef4a8ed10a781`.

**Cambio (RB-015):** `snapshot.json → health.delay_coverage` ya no cuenta los
viajes suprimidos enteros (`CANCELED`) ni en el numerador ni en el denominador;
los acortados (`SKIPPED`) sí cuentan. El bloque gana `n_cancelados` (aditivo).
Quita el crítico falso del 29/10 (52/65, 80 %, con los 13 sin retraso
suprimidos). Runbook: `docs/runbooks/RB-015-delay-coverage-cancelados.md`.

Verificación: ejecuciones de la generación 20 de 18:25 a 18:30 (seis) OK, sin
ninguna línea `severity>=ERROR`. Entre ~18:18:30 y ~18:31 Renfe publicó Madrid
a ratos (servicio 1001J; RB-002, empezó antes del despliegue): las pasadas vacías
conservaron el último bueno (`stale_since` temporal, nunca `degraded`). Ya con
Madrid estable, snapshot de 18:30:50 (<60 s) sin `degraded` ni `stale_since`,
toda la salud `ok`, y `delay_coverage` = `ok`, «48/48 (100.0%) tripUpdate con
retraso publicado (32 suprimidos excluidos)», `n_cancelados: 32`. `boards.json`
fresco (90 estaciones con salidas, `schedule_days_left = 29`,
`departures_contradicting_live = 0`).

## Despliegue anterior (2026-10-27, RB-014 / PR #15)

Con el OK de Juan (27/10, 20:02 Madrid): PR #15 mergeado como `374c116`
(20:04), `cercanias-ingesta` reconstruido desde un `git archive` limpio de
`374c116` con `gcloud run jobs deploy cercanias-ingesta --source .
--update-env-vars ARCHIVAR_CANCELACIONES=1,ARCHIVAR_AVISOS=1` (listo a las
20:07). Diff del `describe` antes/después: sólo la imagen y las dos banderas;
resto de env, secreto de Telegram, SA, recursos y Scheduler iguales. Después se
mergearon PR #10 (`21c2188`, 20:15) y PR #16 (`6f8169b`, 20:17): sólo histórico
(`scripts/construir_historico.py`, `cercanias_domain/historico*.py`), no tocan
la ingesta, así que no hubo que reconstruirla. API y web sin cambios.

| Pieza | Ahora | Rollback |
|---|---|---|
| Job `cercanias-ingesta` | `cercanias-ingesta@sha256:b747dd76…bc9e8` (PR #15, `374c116`), `ARCHIVAR_CANCELACIONES=1`, `ARCHIVAR_AVISOS=1` | Banderas: `gcloud run jobs update cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora --remove-env-vars ARCHIVAR_CANCELACIONES,ARCHIVAR_AVISOS` (RB-014). Imagen: `--image …/cercanias-ingesta@sha256:ee6d9dadcb7b5795d162f9580d8ae2895cdb44d2a9dd17eada96ea81981934f2` (PR #11, `0a6dc68`); antes `…@sha256:646a4d8b871226afb99de7f7d15a72a9f9196ae6397d776161227bfb84083294` |

Ingesta: `europe-southwest1-docker.pkg.dev/cercanias-en-hora/cloud-run-source-deploy/cercanias-ingesta@sha256:b747dd76934340ef44373aa29c8f79d361115aee8ce0105de7156adabdebc9e8`.
Env de rollback (antes del 27/10): `SNAPSHOT_BUCKET_GCS=cercanias-madrid-rt-web`,
`PRIVATE_BUCKET=cercanias-madrid-rt-privado`, `RENFE_NUCLEO_CODES=10`,
`TELEGRAM_CHAT_ID=@cercanias_madrid_rt`, `TELEGRAM_BOT_TOKEN_SECRET=…/telegram-bot-token/versions/latest`,
`ARCHIVE_ENABLED=1`; 512Mi, 1 CPU, timeout 300 s, `maxRetries` 1.

Verificación (RB-014): ejecuciones de 20:07 a 20:16 OK con la imagen nueva,
sin ERROR; snapshot <60 s, sin `degraded`, salud toda `ok`; `boards.json`
fresco con 95 estaciones y ~830 salidas. Primera copia de avisos en
`archivo/alerts/2026-10-27/200802.json` (15 avisos de Madrid) y
`alerts-state.json` creado; ninguna más (feed congelado desde el 07/10: un
WARNING `alerts_feed_stale`, esperado; `boards.json → health.alerts_feed_stale: true`).
Las pasadas de `archivo/<día>/` llevan `canc` y `skip`.

El incremental diario del histórico se lanza ahora desde `main`
(`/workspace/cercanias-madrid-rt`), no desde la rama de PR #10.

## Despliegue de 2026-10-25 (RB-011 / PR #11)

`main` = producción para la ingesta: PR #11 mergeado como `0a6dc68` (CI verde) y
`cercanias-ingesta` reconstruido desde un `git archive` limpio de `0a6dc68` el
25/10 entre las 08:27 y las 08:29 (Madrid), con el OK de Juan, con
`gcloud run jobs deploy cercanias-ingesta --source .`. Sólo cambió la imagen:
env, secretos, SA y Scheduler iguales (diff del `describe` antes/después).
API y web siguen como en RB-009 (PR #9, ya mergeado en `251ef08`).

| Pieza | Ahora | Rollback |
|---|---|---|
| API `cercanias-api` | revisión `cercanias-api-00007-vcv` (`cercanias-api@sha256:44a51d4f…aa85`) | `gcloud run services update-traffic cercanias-api --region europe-southwest1 --to-revisions cercanias-api-00006-v2z=100` |
| Job `cercanias-ingesta` | `cercanias-ingesta@sha256:ee6d9dad…1934f2` (PR #11, `0a6dc68`) | `gcloud run jobs update cercanias-ingesta --region europe-southwest1 --image europe-southwest1-docker.pkg.dev/cercanias-en-hora/cloud-run-source-deploy/cercanias-ingesta@sha256:646a4d8b871226afb99de7f7d15a72a9f9196ae6397d776161227bfb84083294` (RB-009, `42234c2`); antes de esa, `…@sha256:d44a820af87f323f4832abdf1a98264b3a1994542d5dc15f51c5e0ccf9694f7f` |
| Web `index.html` | generación `1792684768397146` (= `apps/web/index.html` de `42234c2`) | subir `apps/web/index.html` de `72d72a0` con `--cache-control="public, max-age=60" --content-type="text/html"` (antes: generación `1792676247042413`) |

API: imagen completa `europe-southwest1-docker.pkg.dev/cercanias-en-hora/cloud-run-source-deploy/cercanias-api@sha256:44a51d4f7ef08f21b22293ff4037b84e19bd818b786c9fd57a5ab9b6ca09aa85`
(anterior `…@sha256:4858e26969f1a8ee07d00caf7f83783f2ebaceda7223b13c8c4a806981b7d807`).
Ingesta: `europe-southwest1-docker.pkg.dev/cercanias-en-hora/cloud-run-source-deploy/cercanias-ingesta@sha256:ee6d9dadcb7b5795d162f9580d8ae2895cdb44d2a9dd17eada96ea81981934f2`.

**PR #11 (RB-011):** si la última pasada de una invocación viene vacía y se
conserva una anterior de la misma invocación con menos de
`STALE_WARNING_SECONDS` (240 s), ya no se marca `degraded`; más vieja, sí.
Verificación tras el despliegue: ejecuciones de 08:30, 08:31, 08:32 y 08:33 OK con la
imagen nueva; a las 08:32 la pasada 3/3 vino vacía y se publicó sin `degraded`
(«pasada final vacia: se conserva la anterior (20s), aun vigente»); snapshot de
~60 s con 91 trenes y salud ok; `boards.json` fresco
(`departures_contradicting_live = 0`, `schedule_days_left = 29`). Runbook:
`docs/runbooks/RB-011-degraded-with-fresh-data.md`.

**Hecho 6, medido (RB-009, desplegado):** con el tren aún en su
origen (`STOPPED_AT`), el 54 % de los «0» de Renfe salieron ≥3 min tarde
(19-22/10/2026, n=2.387; C4 67 %). Ese estado es `pending_departure`, nunca
«en hora». La salida es la transición a `IN_TRANSIT_TO`.

**Matiz al hecho 6 (anterior):** desde RB-008 un viaje que está en el feed manda aunque su
`delay` sea 0; el aviso de entrante sólo sale sin dato propio y en líneas con
emparejamiento validado (hoy C1). Ver `docs/runbooks/RB-008-bot-boards-inconsistent.md`.

## Estado actual

- 561 tests, cobertura 98,23 % (27/10, tras PR #16). `make ci` verde.
- Horarios: los regenera cada noche `cercanias-horarios` (Scheduler
  `cercanias-horarios-nightly`). El GTFS de Renfe trae ~30 días por delante.
  **Incidente:** los subidos a mano en agosto caducaron hacia el 29/08 y los
  tablones estuvieron vacíos hasta el 14/10 sin que ningún job fallara.
- Archivo histórico: >5.700 pasadas desde el 20/08, en el bucket privado.
  **Retención: 90 días** desde el 23/10/2026 (lo pidió Juan; antes, regla de
  borrado `age 45` sobre el prefijo `archivo/`). Rollback:
  `gcloud storage buckets update gs://cercanias-madrid-rt-privado --lifecycle-file=/workspace/historico/lifecycle_before.json`.
  `make analizar` lo resume.
- **Histórico de retrasos** (23/10/2026): `historico/<día>.parquet` + `historico/calidad/<día>.json`
  en el bucket privado, una fila por (día, tren, parada), **sin regla de borrado**.
  Rellenado del 08/09 al 22/10. Se construye con `scripts/construir_historico.py`;
  definición de «retraso real», calidad y rollback en
  `docs/runbooks/RB-010-historico.md`. Aún sin job programado: el incremental
  diario (`... construir_historico.py ayer`) se lanza a mano o desde el box.
- Feedback del bot: bucket **privado**, `make feedback`.

## Trabajo pendiente, por prioridad

1. ~~Automatizar la regeneración de horarios~~ — hecho el 14/10 (ver
   `docs/DESPLIEGUE.md`). **Falta** crear la política de alerta sobre
   `severity>=ERROR` en Cloud Monitoring.
2. **Invariante de cobertura** que avise ANTES de caducar, no después. Parcial:
   `boards.json` ya expone `health.schedule_days_left` y `regenerate-schedules`
   falla con menos de 25 días; falta alertar con `schedule_days_left < 5`. Encargo
   preparado para grok en `brief-grok-guardia.md`. El aviso actual sólo salta el
   día que ya no hay horario.
3. **Verificar supresiones con datos de Madrid.** `CANCELED`/`SKIPPED` ya se
   parsean y se muestran, pero sólo se han visto en otros núcleos: el 21/08
   Madrid llevaba horas ausente del feed.
4. **Semana de archivo** (a partir del 27/08): medir cuánto retraso sobrevive a
   la maniobra y con qué frecuencia se suprime un servicio. Hoy el umbral usa una
   maniobra mínima de 3 min elegida a ojo.
5. **AWS sin desplegar** y sin comparativa de rendimiento. Los adaptadores ya son
   portables (`SNAPSHOT_BUCKET_S3`). **La cuenta sigue usando credenciales root**;
   el plan acordado es que el primer `terraform apply` cree un usuario IAM.

## Trampas para quien continúe

- **Verificar contra el feed real, no sólo con tests.** Los dos fallos más graves
  —el emparejamiento 1:1 y las cabeceras sin limpiar— pasaron con la suite verde.
- **Cuidado al declarar algo imposible.** «No se pueden saber las supresiones»
  fue falso y condicionó semanas de diseño.
- **Los avisos que saltan siempre no se leen.** Pasó con «datos no actualizados»
  sobre datos de 78 segundos.
- **Un fallo silencioso es peor que un error.** Casi todo lo que ha salido mal
  aquí devolvía cero o una hora limpia en vez de fallar.
