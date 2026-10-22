# Estado del proyecto y traspaso

Documento de continuidad. Se escribe para que otra sesión pueda seguir sin
reconstruir el contexto. **Última actualización: 2026-10-22.**

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

## Despliegue actual (2026-10-22, RB-008)

Producción corre la rama `feat/estimate-from-inbound` en `03c0967` (PR #7 +
PR #8 + RB-008), **aún sin mergear en `main`**. Desplegado el 22/10 entre las
15:30 y las 15:37 (Madrid) con el OK de Juan, en el orden de RB-008:

| Pieza | Ahora | Rollback (anterior, = `main`) |
|---|---|---|
| API `cercanias-api` | revisión `cercanias-api-00006-v2z` (`cercanias-api@sha256:4858e269…d807`) | `gcloud run services update-traffic cercanias-api --region europe-southwest1 --to-revisions cercanias-api-00005-7sk=100` |
| Job `cercanias-ingesta` | `cercanias-ingesta@sha256:d44a820a…94f7f` | `gcloud run jobs update cercanias-ingesta --region europe-southwest1 --image europe-southwest1-docker.pkg.dev/cercanias-en-hora/cloud-run-source-deploy/cercanias-ingesta@sha256:6df10a54004ba6ba6e7065da3716ef0db595190ccc4eece9638cf9bd8b56f8c8` |
| Web `index.html` | generación `1792676247042413` (= `apps/web/index.html` de `03c0967`) | subir `apps/web/index.html` de `main` (`a7b11ac`) con `--cache-control="public, max-age=60" --content-type="text/html"` (antes: generación `1792504396121694`) |

API: imagen completa `europe-southwest1-docker.pkg.dev/cercanias-en-hora/cloud-run-source-deploy/cercanias-api@sha256:4858e26969f1a8ee07d00caf7f83783f2ebaceda7223b13c8c4a806981b7d807`
(anterior `…@sha256:eadb687959e9be4e30ca81e0932d64b063f40d006cdd7e2dfb1b116fc6c0a96f`).
Ingesta: `…cercanias-ingesta@sha256:d44a820af87f323f4832abdf1a98264b3a1994542d5dc15f51c5e0ccf9694f7f`.
Env, secretos, cuenta de servicio y Scheduler sin cambios. Verificación tras el
despliegue: `/health` ok, `boards.json` con `display` en 853/853 filas, 95
estaciones, `health.departures_contradicting_live = 0`, snapshot fresco (~20 s).
Cuando se mergeen PR #7 y #8, `main` vuelve a coincidir con producción.

**Matiz al hecho 6:** desde RB-008 un viaje que está en el feed manda aunque su
`delay` sea 0; el aviso de entrante sólo sale sin dato propio y en líneas con
emparejamiento validado (hoy C1). Ver `docs/runbooks/RB-008-bot-boards-inconsistent.md`.

## Estado actual

- 276 tests, cobertura 97,13 %. `make ci` verde.
- Horarios: los regenera cada noche `cercanias-horarios` (Scheduler
  `cercanias-horarios-nightly`). El GTFS de Renfe trae ~30 días por delante.
  **Incidente:** los subidos a mano en agosto caducaron hacia el 29/08 y los
  tablones estuvieron vacíos hasta el 14/10 sin que ningún job fallara.
- Archivo histórico: >5.700 pasadas desde el 20/08, en el bucket privado, con
  regla de borrado a 45 días. `make analizar` lo resume.
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
