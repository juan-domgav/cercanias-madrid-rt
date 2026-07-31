# ADR-010 — El código de núcleo `1080` del Anexo A.2 está obsoleto

- **Estado:** Aceptada
- **Fecha:** 2026-07-31
- **Afecta a:** Anexo A.2, C-3, y la regla que el README califica como la primera de
  «las tres cosas que más importan»
- **Severidad:** crítica. Aplicada al pie de la letra, la regla del Anexo A.2 devuelve
  **cero** trenes.

## Contexto

Anexo A.2 fija la regla de filtrado del núcleo de Madrid como prefijo `1080` en `tripId`
más el patrón `SPECIAL_10_*`, y C-3 la eleva a instrucción literal:

> regla literal del Anexo A.2 — prefijo `1080` en `tripId` **más** el patrón
> `SPECIAL_10_*`. Prohibido filtrar por etiqueta de línea.

El Anexo se construyó sobre una captura en vivo del **2026-07-29**.

## Hecho medido

Descarga en vivo de `https://gtfsrt.renfe.com/vehicle_positions.json` el
**2026-07-31**, contando prefijos de 4 dígitos sobre las 272 entidades del payload:

```
1010: 99      5110: 62      2010: 26      4010: 16
6210: 14      6010: 10      4110: 10      3010:  8
3110:  6      3210:  6      4510:  4      6110:  3

1080 →  0 entidades
1010 → 99 entidades
```

**El prefijo `1080` no aparece en el feed.** Ni una sola entidad.

El desplazamiento es sistemático, no un caso aislado de Madrid: **todos** los prefijos
observados terminan en `10` donde el Anexo A.2 documenta `80`. El patrón es sustituir
los dos últimos dígitos `80` por `10`:

| Núcleo | Anexo A.2 | Observado en vivo |
|---|---|---|
| Madrid | `1080` | `1010` |
| Sevilla | `3080` | `3010` |
| Valencia | `4080` | `4010` |
| Barcelona | `5180` | `5110` |
| Bilbao | `6080` | `6010` |
| Santander | `6280` | `6210` |
| Zaragoza | `7080` | `7010` |

## Corroboración independiente

Que `1010` es efectivamente Madrid no se apoya sólo en el patrón aritmético:

1. Las **51 estaciones distintas** vistas tras filtrar por `1010` son inequívocamente de
   Cercanías Madrid: Chamartín, Atocha, Móstoles, Alcalá de Henares, Aranjuez, Parla,
   Guadalajara, Pitis, Aeropuerto T4.
2. Las **51 de 51** correlacionaron por `exact_code` contra la capa
   `Tramificacion:Dependencias` de IDEADIF, con `confidence = 1.00`. Una coincidencia del
   100 % contra el registro oficial de dependencias de Adif no es compatible con un
   filtro que estuviese recogiendo otro núcleo.
3. Las líneas detectadas (`C1`…`C10`, con los sufijos `C4a`/`C4b` y `C8a`/`C8b`) son
   exactamente las de la red de Madrid.

## Decisión

1. `DEFAULT_NUCLEO_CODES` incluye `("1080", "1010")`. Se mantienen **ambos**:
   - `1080` conserva la compatibilidad con los fixtures congelados del Anexo A, que son
     la entrada de los tests de contrato y no deben reescribirse.
   - `1010` es el único que produce datos contra el feed real de hoy.
2. Los códigos siguen siendo configurables por `RENFE_NUCLEO_CODES` (C-3 exige que el
   código de núcleo sea configurable, no una constante).
3. `data/mappings/nucleos.yaml` publica la tabla completa Anexo A ↔ observado, con el
   nivel de confianza de cada fila, distinguiendo lo verificado por coordenadas de lo
   inferido por patrón aritmético.

## Consecuencias

- **La jerarquía de fuentes de verdad tiene un límite temporal.** El Anexo A prevalece
  sobre la especificación porque describe el comportamiento real observado; pero una
  observación caduca. Cuando el feed contradice al Anexo, gana el feed, y el hallazgo
  debe volver al Anexo — no quedarse en un informe.
- Cualquier implementación futura que copie la regla del Anexo A.2 literalmente, sin
  leer este ADR, producirá un sistema que no muestra ningún tren y que además **no
  fallará**: filtrará correctamente a cero. Es el modo de fallo más caro posible.
- Queda sin determinar **cuándo y por qué** cambió la numeración entre el 29 y el 31 de
  julio de 2026, y si `1080` fue alguna vez correcto o el Anexo lo derivó de otra fuente.
  No es bloqueante para la Fase 1, pero conviene resolverlo antes de fijar la lista de
  núcleos para producción.

## Acción pendiente

Trasladar este hallazgo a `ANEXO-A-Hechos-Verificados-Feed.md` (sección A.2). El runbook
es explícito en que las tareas de Fase 0 «deben ampliar el Anexo A con lo que se
encuentre, no quedarse en el informe». No se ha modificado el Anexo en esta entrega
porque vive fuera del repositorio, en el paquete de documentación.
