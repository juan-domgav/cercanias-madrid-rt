# RB-012 — Enlace de vuelta (`from`) con una llegada demasiado justa: maniobra mínima por cabecera

> Borrador (PR en draft). No está desplegado.

## Síntoma
En una cabecera el `from` de una salida es una llegada que entra 1-4 min antes,
pero ese tren físico no da la vuelta en esa salida sino en la siguiente. El aviso
«viene con +N» o la estimación salen del tren equivocado. Ejemplo: Móstoles-El Soto,
21/10/2026: el 19545 llega a las 07:42 (CEST) y no forma el 20754 de las 07:45, sino
el 19564 de las 07:50.

## Causa
`_link_turnarounds` (`apps/worker/src/cercanias_worker/regenerate_schedules.py`)
enlazaba cada salida con la llegada sin consumir más próxima con maniobra **≥ 0**.
Ahora exige **≥ N** minutos, con N por estación de cabecera
(`DEFAULT_MIN_TURNAROUND_BY_TERMINUS`, 0 si no figura = como antes). La misma N
se usa al marcar empates (`amb`).

| cabecera | parada | N |
|---|---|---|
| Colmenar Viejo (C4b) | 17005 | 5 |
| Guadalajara (C2) | 70200 | 5 |
| Aranjuez (C3) | 60200 | 3 |
| resto | — | 0 |

Con `INBOUND_ESTIMATE_EXCLUDED_ROUTES` por defecto (C2-C10 fuera) solo cambian
`from`/`slack` publicados; lo que ve el viajero no cambia hasta que se
habilite la estimación en esas líneas.

## Checks
```bash
# ¿qué tabla usa el job? (vacía = comportamiento anterior)
gcloud run jobs describe cercanias-horarios --region europe-southwest1 --format=json \
  | jq '.spec.template.spec.template.spec.containers[0].env'
# backtest sobre el archivo privado (lectura): ajuste 4 días, validación otros 4
uv run python scripts/backtest_emparejamiento_vuelta.py ARCH HORARIOS \
  --ajuste 2026-10-14=1085X 2026-10-15=1086J 2026-10-16=1087V 2026-10-19=1090L \
  --validacion 2026-10-20=1091M 2026-10-21=1092X 2026-10-22=1093J 2026-10-23=1094V
```
La primera línea del backtest comprueba que con N=0 se reproduce el `from`
publicado (1186-1187 de 1198-1199 viajes por día; las 12 diferencias son de
Cercedilla C8/C9 interlineado, El Escorial y Chamartín C2, todas con N=0).

## Vuelta atrás
Sin desplegar código: `MIN_TURNAROUND_BY_TERMINUS=` (vacía) en el job
`cercanias-horarios` y relanzarlo; o una tabla distinta (`"70200=5"`). Con código:
revertir el PR y regenerar horarios.

## Límites conocidos
- La N sale de 4 días laborables de octubre (n≈200-230 salidas por cabecera en
  validación, 45-65 de ellas con retraso real ≥3). Poca muestra; revisar con más días.
- El enlace es de horario: Renfe cambia material (2-7 % de enlaces físicamente
  imposibles en cualquier k) y con retrasos grandes el tren no da la vuelta prevista.
- Decir «0» siempre acierta más (60 %) que cualquiera de los dos enlaces (46-50 %):
  la estimación desde el entrante ya estaba por debajo de esa referencia.
