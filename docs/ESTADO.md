# Estado del proyecto y traspaso

Documento de continuidad. Se escribe para que otra sesión pueda seguir sin
reconstruir el contexto. **Última actualización: 2026-08-21.**

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

## Estado actual

- 266 tests, cobertura 97,13 %. `make ci` verde.
- Horarios cubren **1031V..1060S** (30 días deslizantes desde hoy).
- Archivo histórico: >5.700 pasadas desde el 20/08, en el bucket privado, con
  regla de borrado a 45 días. `make analizar` lo resume.
- Feedback del bot: bucket **privado**, `make feedback`.

## Trabajo pendiente, por prioridad

1. **Automatizar la regeneración de horarios.** `regenerate_schedules.py` ya
   existe (lo escribió codex, revisado y corregido: le faltaba limpiar las
   cabeceras y era demasiado estricto emparejando). **Falta programarlo** en
   Cloud Scheduler, dentro del hueco sin servicio (~01:30–05:00). Sin esto, los
   horarios caducan el 1060S y la app se queda sin trenes otra vez.
2. **Invariante de cobertura** que avise ANTES de caducar, no después. Encargo
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
