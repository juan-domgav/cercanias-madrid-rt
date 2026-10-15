# RB-004 — `schedule_days_left` bajo en falso al envolver 1099→1001

## Sintoma
`health.schedule_days_left` cerca de 0…2 aunque el bucket tiene ~30 ficheros, en
días alrededor del salto `1099X` → `1001J`. Riesgo de alerta prematura o de
ignorar un valor «bajo» que sí es real.

## Causa
El índice numérico (dígitos 3–4) envuelve de 99 a 01 **saltando 00**. Contar
«días por delante» solo con aritmética modular del número, o con `% 100`, deja
un hueco y corta la racha. Además pueden convivir ficheros viejos con el mismo
número y **otra letra** (`1010V` de agosto junto a `1010S` nuevo).

## Checks
```bash
curl -s "$WEB/boards.json" | jq '{service,health}'
gcloud storage ls gs://cercanias-madrid-rt-web/horarios/ | sort
# cadena correcta: mismo nucleo, índice +1 (99→01) y letra del día siguiente (X→J→V…)
```

## Fix
El código debe avanzar por **id completo** (número + letra), no solo por el
entero. Si el valor en vivo miente: regenerar horarios (RB-001/RB-003) y
esperar el siguiente tick; no borrar ficheros a mano sin listar antes.

## Verify
Con servicio `1097L` y ficheros `1097L…1099X,1001J,1002V` → `schedule_days_left ≥ 4`.
Tras regenerar, no deben quedar ids huérfanos que rompan la cadena esperada.

## Rollback
N/A (métrica de solo lectura). Si una alerta dispara por falso bajo, silenciar
la política hasta el siguiente ciclo de ids fuera de la zona de envoltura.
