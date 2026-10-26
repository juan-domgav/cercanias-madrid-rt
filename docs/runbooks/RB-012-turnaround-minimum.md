# RB-012 — Enlace de vuelta (`from`) con una llegada demasiado justa: maniobra mínima por cabecera

> **Estado: mecanismo desplegable, tabla VACÍA.** Ningún valor está aprobado.
> Con la tabla por defecto (`{}`) y la variable sin definir, `horarios/*.json` es
> byte a byte igual que antes (comprobado en 30 días de servicio, misma sha256).
> No activar valores hasta cumplir [Antes de activar valores](#antes-de-activar-valores).

## Síntoma
En una cabecera el `from` de una salida es una llegada que entra 1-4 min antes,
pero ese tren físico no da la vuelta en esa salida sino en la siguiente. El aviso
«viene con +N» o la estimación salen del tren equivocado. Ejemplo: Móstoles-El Soto,
21/10/2026: el 19545 llega a las 07:42 (CEST) y no forma el 20754 de las 07:45, sino
el 19564 de las 07:50.

## Causa y mecanismo
`_link_turnarounds` (`apps/worker/src/cercanias_worker/regenerate_schedules.py`)
enlaza cada salida con la llegada sin consumir más próxima con maniobra **≥ 0**.
Con este cambio puede exigir **≥ N** minutos por parada de cabecera
(`DEFAULT_MIN_TURNAROUND_BY_TERMINUS`, hoy `{}`; parada ausente = N 0 = como
antes). La misma N se usa al marcar empates (`amb`). La tabla se puede
sobrescribir sin desplegar código con la variable `MIN_TURNAROUND_BY_TERMINUS`
del job `cercanias-horarios`.

Formato: `PARADA:MIN;PARADA:MIN` (p. ej. `17005:5;70200:5`). `;` entre entradas
y `:` entre parada y minutos, sin `,` ni `=`, para que `gcloud
--update-env-vars` no la parta (gcloud separa pares por `,`; si alguna vez hace
falta una coma, usar el delimitador alternativo `^@^`, ver `gcloud topic escaping`).

Es a prueba de fallos:
- Si la variable está mal formada (entrada sin `:`, `=` o `,` como separador,
  minutos no enteros, negativos, `+5`, `5.0`, > `MAX_TURNAROUND_MINUTES` (40),
  parada repetida…), el job registra un `ERROR` estructurado
  (`component: regenerate-schedules`, `value: …`), usa la tabla **vacía entera** y
  publica igual. El job no falla.
- Paradas que no son origen de ningún viaje de Madrid en el GTFS: `ERROR` con
  `unknown_stops: [...]`; se ignoran y el resto de la tabla se aplica.

Relacionado (RB-007): en las líneas de `INBOUND_ESTIMATE_EXCLUDED_ROUTES`
(por defecto C2, C3, C4, C5, C7, C8, C9, C10) ya no se usa el entrante para
estimar, y desde este PR **tampoco para el aviso «🚫 SUPRIMIDO»** del entrante
(`entrante_suprimido`, `packages/domain/src/cercanias_domain/departures.py`).
Así un `from` cambiado nunca llega al viajero en esas líneas. Ese corte sí
cambia lo que ve el viajero hoy: ver «Límites conocidos».

## Checks (solo lectura)
```bash
# ¿qué tabla usa el job? (sin MIN_TURNAROUND_BY_TERMINUS = tabla vacía = como main)
gcloud run jobs describe cercanias-horarios \
  --region europe-southwest1 --project cercanias-en-hora --format=json \
  | jq '.spec.template.spec.template.spec.containers[0].env'

# ¿errores de la tabla en la última ejecución?
gcloud logging read \
  'resource.type="cloud_run_job" AND resource.labels.job_name="cercanias-horarios"
   AND severity>=ERROR AND jsonPayload.component="regenerate-schedules"' \
  --project cercanias-en-hora --freshness 2d --limit 20 \
  --format='value(timestamp,jsonPayload.message,jsonPayload.value,jsonPayload.unknown_stops)'

# backtest sobre el archivo privado (local): 4 días de ajuste, 4 de validación
uv run python scripts/backtest_emparejamiento_vuelta.py ARCH HORARIOS \
  --ajuste 2026-10-14=1085X 2026-10-15=1086J 2026-10-16=1087V 2026-10-19=1090L \
  --validacion 2026-10-20=1091M 2026-10-21=1092X 2026-10-22=1093J 2026-10-23=1094V \
  [--fijo '17005:5;70200:5']
```
La primera línea del backtest comprueba que con N=0 se reproduce el `from`
publicado (1186-1187 de 1198-1199 viajes por día; las 12 diferencias son de
Cercedilla C8/C9 interlineado, El Escorial y Chamartín C2, todas con N=0).

Métrica dura del backtest: **retrasos reales (≥3 min) mostrados «≈ en hora»**
(estimación < 3), por cabecera y total, A=5 y A=10, frente a la base N=0 y frente
a «suponer en hora» (= todas las salidas con retraso real). Si una tabla la sube
en cualquier cabecera o en el total, imprime `RESULTADO: FAIL` y sale con código
≠ 0. Al ajustar, descarta las N que la suben.

Resultado con el archivo 14-23/10/2026 (laborables):

| tabla | en hora A=10, validación (N=0→tabla) | en hora A=10, ajuste | resultado |
|---|---|---|---|
| `17005:5;70200:5;60200:3` (borrador anterior del PR) | TODAS 568→593; 70200 0→14; 17005 23→29; 60200 18→23 | TODAS 553→584 | **FAIL** (exit 1) |
| ajuste con la métrica dura | elige `{}` (todas las N>0 suben la métrica en alguna cabecera) | — | OK |
| `{}` (por defecto) | sin cambios | sin cambios | OK (exit 0) |

«Suponer en hora» muestra los 1673 retrasos reales de validación como en hora;
la estimación por entrante con N=0, 568 (A=10).

## Activar un valor (solo con aprobación, ver abajo)
```bash
# AVISO: usar --update-env-vars. --set-env-vars REEMPLAZA todas las variables del
# job y borraría SNAPSHOT_BUCKET_GCS (DESPLIEGUE.md:107-113): el job publicaría
# en otro sitio o fallaría.
gcloud run jobs update cercanias-horarios \
  --region europe-southwest1 --project cercanias-en-hora \
  --update-env-vars 'MIN_TURNAROUND_BY_TERMINUS=17005:5;70200:5'

# regenerar ya (si no, lo hace cercanias-horarios-nightly a las 05:17 Madrid)
gcloud run jobs execute cercanias-horarios \
  --region europe-southwest1 --project cercanias-en-hora --wait
```
Ojo: volver a desplegar el job con el comando de `DESPLIEGUE.md:107-113`
(`--set-env-vars`) quita la variable en silencio; se vuelve a la tabla vacía.

## Verificar
1. La ejecución termina en 0 y la consulta de logs de «Checks» no devuelve
   `ERROR` de `regenerate-schedules` (si sale «MIN_TURNAROUND_BY_TERMINUS
   invalida, se ignora entera», no se aplicó nada; si sale `unknown_stops`, se
   aplicó sin esas paradas).
2. `env` del job (primer check) muestra la variable con el valor esperado.
3. Los `from` publicados cambian solo en las cabeceras de la tabla:
   ```bash
   WEB=https://storage.googleapis.com/cercanias-madrid-rt-web
   SVC=1097V   # un día de servicio publicado; guardar una copia ANTES de activar
   curl -s "$WEB/horarios/$SVC.json?t=$(date +%s)" \
     | jq -r '.stations["17005"][] | select(.seq==1) | [.t,.trip,.from,.slack] | @tsv'
   # comparar con la copia previa: diff <(jq ... antes.json) <(jq ... despues.json)
   ```

## Vuelta atrás
```bash
gcloud run jobs update cercanias-horarios \
  --region europe-southwest1 --project cercanias-en-hora \
  --remove-env-vars MIN_TURNAROUND_BY_TERMINUS
gcloud run jobs execute cercanias-horarios \
  --region europe-southwest1 --project cercanias-en-hora --wait
```
Sin la variable se usa la tabla por defecto (vacía) y los `horarios` vuelven a
ser idénticos a main. Con código: revertir el PR y regenerar horarios.

## Antes de activar valores
**No hay ningún valor aprobado.** Decidir tras 2-3 semanas más de datos
(archivo privado), que incluyan **fines de semana** y días con **trenes
suprimidos** (cancelaciones), y solo con:
- `RESULTADO: OK` en la métrica dura (ninguna cabecera ni el total muestran más
  retrasos reales «≈ en hora» que con N=0), en ajuste y en validación;
- mejora de F1 de avisos con intervalos que no incluyan 0 y muestra suficiente
  por cabecera (hoy: n≈200-230 salidas, 45-65 con retraso real ≥3);
- aprobación explícita del propietario.

## Límites conocidos
- La tabla anterior (`17005:5;70200:5;60200:3`) subía el F1 en esas 3 cabeceras
  pero con poca muestra; la mayoría de combinaciones cabecera×N tienen
  intervalos que incluyen 0, y la métrica dura la tumba (arriba).
- El enlace es de horario: Renfe cambia material (2-7 % de enlaces físicamente
  imposibles en cualquier k) y con retrasos grandes el tren no da la vuelta prevista.
- Decir «0» siempre acierta más (60 %) que cualquiera de los dos enlaces (46-50 %):
  la estimación desde el entrante ya estaba por debajo de esa referencia.
- El corte de «🚫 SUPRIMIDO» en líneas excluidas (C2-C10 por defecto) **sí cambia
  lo que ve el viajero**: esas salidas de cabecera dejan de avisar de que el
  entrante va suprimido (quedan «sin confirmar» si no hay tren en vivo). Está en
  un commit aparte del PR para poder quitarlo sin tocar lo demás.
