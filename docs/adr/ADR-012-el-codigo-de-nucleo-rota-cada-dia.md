# ADR-012 — Los dos últimos dígitos del `tripId` rotan con el día

- **Estado:** Aceptada
- **Fecha:** 2026-08-01
- **Severidad:** crítica. Es la causa raíz de ADR-010, que sólo describía el síntoma.
- **Sustituye a:** ADR-010 (que sigue siendo válido como registro histórico)

## Cómo se detectó

Al ejecutar `make mvp` la mañana del 1 de agosto, las cinco pasadas dieron
**CRÍTICO**, y la notificación de Telegram se disparó a la tercera:

```
[1/5] 12:43:06  trenes=0  salud=CRITICAL
  ! nucleo_filter_yield: 0 entidades tras el filtro a las 12:43 (banda: valle),
    con 240 entidades en el payload. El feed trae datos pero NINGUNO se reconoce
    como Madrid. NO es 'no hay trenes'.
```

El invariante de WP-1 hizo exactamente su trabajo: distinguir «no hay trenes» de
«he dejado de reconocer los trenes». Sin él, el sistema habría servido un mapa
vacío un sábado a mediodía sin un solo error en los logs.

## Hecho medido

Censo de prefijos del feed en vivo, 2026-08-01 12:45 UTC:

```
'1011':  89    ej: '1011S27455C4b'      <- Madrid
'5111':  56    ej: '5111S25642R1'       <- Barcelona
'2011':  19    '6011': 15   '4011': 12
'6211':   9    '4111':  7   '3011':  5
'3111':   5    '3211':  5   '6111':  5
'4511':   4    '4711':  2   '7011':  2
```

Comparado con los dos días anteriores para el **mismo** núcleo de Madrid:

| Fecha | Día | `tripId` de ejemplo | Prefijo |
|---|---|---|---|
| 2026-07-29 | jueves | `1080J20316C4a` | `1080` |
| 2026-07-31 | viernes | `1010V21348C10` | `1010` |
| 2026-08-01 | sábado | `1011S27455C4b` | `1011` |

## Causa raíz

Los cuatro dígitos iniciales **no son un identificador de núcleo**. Se descomponen:

```
1011S27455C4b
└┬┘└┬┘│└─┬─┘└┬┘
 │  │  │  │  └── etiqueta de línea
 │  │  │  └───── número de tren
 │  │  └──────── letra del DÍA: J=jueves, V=viernes, S=sábado
 │  └─────────── dos dígitos que ROTAN con el calendario
 └────────────── núcleo real, ESTABLE: 10 = Madrid
```

La letra separadora lo confirma de forma independiente: `J`→`V`→`S` es
jueves→viernes→sábado. Los dos dígitos rotan en paralelo a esa letra.

Que el núcleo son **dos** dígitos ya estaba a la vista en el propio Anexo A.2: el
patrón especial es `SPECIAL_**10**_*`, de dos dígitos, no cuatro. Y el modelo de
dominio ya extraía `nucleo_prefix` describiéndolo como «primeros 2 dígitos,
**siempre disponible**». La información estaba; la usaba el parser pero no el
filtro.

## Por qué ADR-010 no bastaba

ADR-010 observó el cambio `1080`→`1010` y lo trató como una actualización
puntual: añadir el código nuevo a una lista. Esa estrategia es una carrera que se
pierde, porque el valor caduca **cada día**, y se pierde en silencio: un filtro
que no reconoce nada no falla, devuelve cero. La avería de hoy es la segunda en
tres días.

## Decisión

1. `DEFAULT_NUCLEO_CODES = ("10",)`. Se filtra por el **prefijo de 2 dígitos**.
2. `build_nucleo_filter` acepta 2 dígitos (recomendado) o 4 (exacto). Con 2
   genera `^(?:10\d{2}[A-Z]|SPECIAL_10_)`, que casa con `1080J…`, `1010V…` y
   `1011S…` sin tocar configuración.
3. El código de 4 dígitos se conserva **sólo** para fijar un día concreto en un
   test. No debe usarse en producción.
4. `RENFE_NUCLEO_CODES` sigue permitiendo sobrescribirlo (C-3 exige que el código
   de núcleo sea configurable).

Verificado: el filtro reconoce los tres días observados y el patrón especial, y
sigue excluyendo Barcelona, Sevilla, Bilbao, Zaragoza y el resto.

## Consecuencias

- **Las fixtures congeladas siguen valiendo.** `1080J…` empieza por `10`, así que
  los tests de contrato del Anexo A pasan sin reescribirlos.
- Se eliminó un test que exigía que `DEFAULT_NUCLEO_CODES` contuviera `"1080"` y
  `"1010"`. Ese test **codificaba la avería**: convertía en requisito la lista
  manual que hay que dejar de mantener.
- La tabla `nucleos.yaml` con la correspondencia Anexo↔observado pasa a ser
  material histórico. Lo operativo son los dos primeros dígitos.
- El campo `nucleo_code` de 4 dígitos que se guarda en `ParsedTripId` es un dato
  del día, no una identidad. No debe usarse como clave de nada persistente.

## Lo que sigue sin saberse

- **La semántica exacta de los dos dígitos que rotan.** `80`→`10`→`11` no es un
  contador simple ni un día de la semana obvio. Probablemente indexan una versión
  de calendario de servicio del GTFS estático, pero no está verificado.
- Si el prefijo de 2 dígitos es estable a largo plazo o también puede cambiar.
  Nada lo garantiza; por eso el invariante de WP-1 se queda, y es la defensa real:
  el filtro correcto de hoy no protege del cambio de mañana, pero la alarma sí lo
  detecta el mismo día.
- Si los `.pb` usan el mismo esquema.
