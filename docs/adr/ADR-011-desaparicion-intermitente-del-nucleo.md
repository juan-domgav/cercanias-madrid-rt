# ADR-011 — El núcleo de Madrid desaparece del feed de forma intermitente

- **Estado:** Aceptada
- **Fecha:** 2026-07-31
- **Severidad:** alta. Afecta al diseño de la ruta de lectura, no sólo a la ingesta.
- **Relación:** confirma empíricamente el riesgo **R-1** de `ARQUITECTURA-v2-y-premortem.md`
  y justifica por sí solo el módulo `cercanias_domain.health` (WP-1).

## Hecho medido

Muestreo de `trip_updates.json` **y** `vehicle_positions.json` cada 20 s durante
~26 minutos, 21 muestras:

```
 #  hora UTC   TU total  TU 1010   VP total  VP 1010
 0  15:20:32        281      100        272       97
 1  15:20:53        181        0        175        0   <-- desaparición
 2  15:21:13        282      101        273       98
 3  15:21:33        279      101        269       98
...
19  15:46:17        281      105        271      102
20  15:46:37        282      105        272      102

muestras sin Madrid: 1/21
```

Y antes del muestreo, al intentar capturar un fixture, **cuatro peticiones
consecutivas** devolvieron 181–182 entidades sin un solo `1010`.

### Lo que caracteriza el fallo

1. **Afecta a los dos feeds a la vez.** No es un problema de `trip_updates`: en la
   misma muestra desaparecen tanto los `tripUpdate` como los `vehiclePosition` de
   Madrid. Es el publicador el que deja de emitir el núcleo entero.
2. **El total cae exactamente en la cuota de Madrid**: 281 → 181 y 272 → 175. El
   resto de núcleos sigue publicando con normalidad.
3. **No hay ningún error.** HTTP 200, `content-type` correcto, JSON válido, cabecera
   con `timestamp` fresco. Sólo faltan ~100 entidades.
4. **Frecuencia observada ≈ 5 %** de las peticiones, con episodios que duran al menos
   un minuto (cuatro peticiones seguidas).

## Por qué importa más de lo que parece

Con un sondeo por minuto, alrededor de una pasada de cada veinte vería Madrid vacío.
Sin protección, el sistema:

- **sobrescribiría el snapshot bueno con uno vacío**, y serviría un mapa sin trenes
  durante todo el intervalo de caché;
- no registraría ningún error, porque no lo hay;
- y, si además hubiera notificaciones, podría anunciar una incidencia inexistente
  («no hay circulación») que es peor que no decir nada.

Es exactamente el modo de fallo de R-1, sólo que provocado por el publicador en lugar
de por un cambio de numeración. El síntoma es idéntico y la defensa, también.

## Decisión

1. **La ruta de lectura nunca sustituye un snapshot bueno por uno vacío.** Semántica de
   *last known good*: si la pasada devuelve cero entidades de Madrid mientras el payload
   trae datos de otros núcleos, se conserva el snapshot anterior y se marca como
   `stale_since`. Es la consecuencia arquitectónica principal de este ADR.
2. **`health.nucleo_filter_yield` es CRÍTICO** en esa situación (ya implementado en
   WP-1). El invariante contrasta explícitamente «payload lleno» contra «filtro a cero»,
   que es justo lo que distingue este caso de «no hay trenes de madrugada».
3. **Un episodio aislado no debe alertar.** Con ~5 % de probabilidad por sondeo, alertar
   a la primera genera un aviso cada ~20 minutos y la alarma se acaba silenciando. Se
   exige **persistencia**: N pasadas consecutivas sin Madrid antes de notificar.
4. El fixture `fixtures/trip_updates_sin_madrid.json` es una captura real de este estado
   y se usa como entrada de test.

## Consecuencias

- El repositorio de estado gana el concepto de «última versión buena conocida», que no
  estaba en la especificación.
- La cadena de rumbo tolera huecos: ya se corrigió al arreglar `{current, previous}`,
  y este ADR explica por qué esos huecos ocurren de verdad y no son teóricos.
- Cualquier métrica de cobertura calculada sobre una sola pasada puede estar medida
  durante una desaparición. Los informes deben indicar el número de pasadas.

## Lo que NO sabemos

- **Por qué** ocurre. Puede ser un reinicio del publicador de ese núcleo, una
  invalidación de caché en el CDN de Renfe, o un despliegue parcial. No hay forma de
  distinguirlo desde fuera.
- Si afecta por igual a los `.pb`, que no se muestrearon.
- Si la frecuencia cambia por hora del día o por carga. 21 muestras en 26 minutos de una
  tarde de viernes no permiten extrapolar; el 5 % es un orden de magnitud, no una cifra.
- Si existe correlación con incidencias reales de la red, en cuyo caso el silencio sería
  informativo en lugar de espurio.
