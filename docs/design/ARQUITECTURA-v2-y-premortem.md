# Arquitectura v2 — pre-mortem y plan ejecutable

- **Fecha:** 2026-07-31
- **Estado:** propuesta, pendiente de aprobación
- **Base:** Fase 0 cerrada (E1–E8), revisión cruzada codex/grok, y mediciones en vivo
  del 31/07/2026 documentadas más abajo
- **Destinatarios:** grok y codex como ejecutores; cada paquete de trabajo lleva
  criterio de aceptación verificable

---

## 1. Hechos medidos que cambian el diseño

Todo lo de esta sección está medido, no supuesto. Es la base del pre-mortem.

| Hecho | Medición | Consecuencia |
|---|---|---|
| Los seis endpoints existen | `vehicle_positions`, `trip_updates`, `alerts`, en `.json` **y** `.pb`, los seis HTTP 200 | La especificación asumía protobuf; el código usa JSON. Ambos están disponibles |
| Retrasos de Madrid | 101 `tripUpdate` de núcleo 1010, **98 con `stopTimeUpdate` (97,0 %)** | La funcionalidad central del producto tiene datos |
| **Un solo `stopTimeUpdate` por viaje** | 98 entidades → 98 valores de `delay` | **No hay predicción para toda la línea**: sólo la próxima parada |
| Rango de retrasos | −120 s a +12 180 s (3 h 23 min), 39 valores distintos | Hay que decidir qué se muestra por encima de cierto umbral |
| Códigos de núcleo | `1080` → 0 entidades; `1010` → 99–103 | El identificador cambió en 48 h (ADR-010) |
| Cascada GTFS estático | NAP exige login; CRTM es una SPA de ArcGIS; sólo sirve `ssl.renfe.com` | **Hay una sola fuente**, y es la que A.10 daba por caída |
| `direction_id` | No existe columna en `trips.txt` | Se deriva por orden lexicográfico de `route_id` — convención arbitraria |
| `stop_id` ↔ Adif | 51/51 por código exacto | El mapeo deja de ser el riesgo principal |
| Frescura | 10–24 s de desfase | El umbral de 120 s es viable |
| Estaciones catalogadas | **51**, las vistas en una única captura | La red de Cercanías Madrid tiene bastantes más |

---

## 2. Pre-mortem

> Es julio de 2027. El proyecto se ha abandonado. ¿Qué salió mal?

Ordenado por probabilidad × impacto, no por gravedad teórica.

### R-1 · «La app dejó de mostrar trenes y nadie se enteró» — CRÍTICO

**Qué pasó:** Renfe volvió a cambiar la numeración de núcleos, como ya hizo entre el 29 y
el 31 de julio de 2026. El filtro dejó de encontrar entidades. La app siguió
respondiendo 200, con un mapa vacío. Ninguna alarma saltó, porque *filtrar a cero no es
un error*: es un resultado válido a las 4 de la madrugada.

**Por qué es el riesgo número uno:** no es hipotético. **Ya ocurrió una vez durante la
propia Fase 0**, y sólo se detectó porque alguien contó entidades a mano. El sistema
actual no tiene forma de distinguir «no hay trenes» de «ya no sé reconocer un tren».

**Mitigación:** invariantes de negocio como alarmas, no métricas de infraestructura.
Perfil horario esperado por núcleo; alertar si el recuento sale de banda. Un despliegue
sano a las 08:00 de un martes con 0 trenes en Madrid es una página, no un gráfico.

### R-2 · «La dirección de los trenes estaba mal» — ALTO

**Qué pasó:** `direction_id` no existe en el GTFS de Renfe. Se derivaba ordenando
lexicográficamente los `route_id` que comparten `route_short_name` y asignando 0 y 1.
Eso no guarda relación con la geografía: el sentido es correcto por azar
aproximadamente la mitad de las veces. La UI (§13.3) dibuja iconos según esa dirección.

**Impacto real:** un usuario ve una flecha apuntando a Alcalá cuando el tren va a
Atocha. Es el tipo de error que destruye la confianza en toda la aplicación, porque es
visible, frecuente y evidentemente falso para cualquiera que conozca la línea.

**Mitigación:** derivar el sentido del **vector de movimiento** (se dispone de posición
actual y anterior — es exactamente lo que ya alimenta el rumbo) o de `trip_headsign`
cruzado con la geometría de la línea. Nunca del orden alfabético. Y mientras no sea
fiable, no dibujarlo: `direction_derived=True` debe poder traducirse en «no mostrar
flecha», no en «mostrar flecha con menos confianza».

### R-3 · «El rumbo dejó de funcionar al desplegar» — ALTO

**Qué pasó:** C-4 exige mantener `{current, previous}` por vehículo. El repositorio es
en memoria. La arquitectura de referencia (§2.2, §15, §16) es *scale-to-zero*: Lambda o
Cloud Run que se apagan entre invocaciones, y prohíbe explícitamente Redis y PostgreSQL
gestionado. En Fase 0 el rumbo funciona porque todo ocurre en un proceso. En producción,
cada invocación arranca con el estado vacío y **todos los rumbos son `None` para
siempre**.

**Por qué se pasó por alto:** es una contradicción entre dos partes de la especificación
que nunca se leyeron juntas. Los tests pasan al 99 % porque prueban el dominio, no el
modelo de despliegue.

**Mitigación:** el estado va a DynamoDB (on-demand con TTL) o Firestore, que la propia
especificación sí permite. Son ~100 escrituras por minuto: coste despreciable. Pero hay
que diseñarlo y probarlo con un adaptador real, no dejar el `InMemory` como si fuera la
implementación de producción.

### R-4 · «Los tiempos después de medianoche estaban descuadrados» — ALTO

**Qué pasó:** GTFS expresa horarios que cruzan la medianoche como `25:14:00`, no como
`01:14:00` del día siguiente. Es la trampa clásica del formato. No hay ni una línea en
todo el código de Fase 0 que la trate, porque Fase 0 no tocó `stop_times.txt` para
calcular horarios. Sumado a que Madrid está en CET/CEST con dos cambios de hora al año,
los servicios nocturnos y los dos domingos de cambio horario dan resultados absurdos.

**Mitigación:** decidir explícitamente la política de tiempo (todo en UTC internamente,
`Europe/Madrid` sólo en presentación) y tratar `>24:00:00` en el parser de
`stop_times.txt`, con tests que incluyan un servicio nocturno y un cambio de hora.

### R-5 · «Sólo sabíamos el retraso de la próxima parada» — ALTO

**Qué pasó:** medido hoy, cada `tripUpdate` de Madrid trae **un solo** `stopTimeUpdate`.
El producto prometía «¿cuándo llega mi tren a mi estación?», pero el feed sólo dice
cuánto lleva de retraso respecto a su próxima parada, que puede estar a diez estaciones
de la del usuario.

**Mitigación:** modelar explícitamente la propagación: retraso conocido en la próxima
parada + tiempos teóricos de `stop_times.txt` = estimación aguas abajo, **marcada como
estimación**. Es una decisión de producto además de técnica, y hay que tomarla antes de
diseñar la UI, no después.

### R-6 · «El origen de datos se cayó y no teníamos alternativa» — MEDIO-ALTO

**Qué pasó:** la cascada de tres orígenes de C-1 es ficción. NAP exige autenticación,
CRTM es una SPA sin URL directa. La única fuente operativa es `ssl.renfe.com`, cuyo modo
de fallo documentado (certificado caducado) ya ocurrió una vez.

**Mitigación:** dejar de tratar el GTFS estático como una descarga en caliente. Es un
artefacto versionado que cambia unas pocas veces al año: se descarga, se valida, se
almacena, y el sistema arranca desde el último bueno conocido aunque el origen esté
caído. Resolver el acceso a NAP (¿hay API con credenciales?) es trabajo aparte.

### R-7 · «El catálogo de estaciones estaba incompleto» — MEDIO

**Qué pasó:** el catálogo se construyó con las **51 estaciones que aparecieron en una
captura**. Las que no tenían tren circulando en ese instante —servicios nocturnos, ramas
de baja frecuencia, estaciones en obras— no entraron. La app no sabía mostrarlas.

**Mitigación:** el catálogo debe derivarse de `stops.txt` filtrado por la red de Madrid,
no de lo observado en tiempo real. Lo observado sirve para *validar* el catálogo, no para
construirlo.

### R-8 · «Un cambio de formato del feed tumbó la ingesta entera» — MEDIO

**Qué pasó:** el parser asume formas (`entity` es lista, `vehicle` es objeto). Una
respuesta malformada lanza `AttributeError` y se pierde el ciclo completo, no una
entidad. Además sólo se traduce `httpx.HTTPError`, así que un origen accesible pero
corrupto se salta el fallback documentado.

**Mitigación:** validación estricta del sobre + parseo defensivo **por entidad**: una
entidad mala se descarta y se cuenta, nunca tumba el lote.

### R-9 · «Nos apoyamos en un endpoint no documentado» — MEDIO

**Qué pasó:** todo el sistema consume los `.json`, que no son el formato estándar
GTFS-RT y no están documentados por Renfe. Existen los `.pb` (medido hoy). El día que
Renfe retire la comodidad JSON, no hay ingesta.

**Mitigación:** implementar protobuf como camino principal —es lo que la especificación
pedía— y dejar JSON como alternativa, con un test de equivalencia campo a campo entre
ambos. Sigue siendo una de las incógnitas abiertas del README.

### R-10 · «El 99 % de cobertura no impidió nada» — MEDIO

La suite ejerce caminos felices y anomalías ya conocidas. No hay ni un fixture hostil.
Las dos vulnerabilidades ALTAS de la revisión de seguridad habrían pasado los 110 tests
sin inmutarse. La cobertura de líneas mide qué se ejecutó, no qué se resistió.

### R-11 · Legal y licencias — MEDIO, subestimado

Los contenidos de adif.es **no** están bajo licencia abierta. El repositorio es público.
En Fase 2 entra contenido de usuario (moderación, LSSI, RGPD para suscripciones). Ningún
paquete de trabajo cubre esto y es el tipo de riesgo que no se arregla con un parche.

---

## 3. Cambios de arquitectura propuestos (v2)

Cinco cambios; el resto de la especificación se mantiene.

**V2-1 · Los invariantes de negocio son de primera clase.** Un módulo `health` que
evalúa expectativas («a las 08:00 de un laborable debe haber entre N y M trenes en
Madrid», «≥90 % de los trip_id resuelven», «≥90 % de los tripUpdate traen delay») y
falla ruidosamente. Esto es lo que convierte R-1 de invisible en detectable.

**V2-2 · El estado se externaliza desde el principio.** El puerto
`CurrentStateRepository` ya existe: añadir un adaptador real (DynamoDB/Firestore con
TTL) y tratar `InMemory` como lo que es, un doble de test. Sin esto, R-3 se descubre el
día del despliegue.

**V2-3 · El GTFS estático es un artefacto, no una descarga.** Descarga → validación →
almacenamiento versionado → arranque desde el último bueno conocido. Elimina R-6.

**V2-4 · La procedencia viaja con el dato, siempre.** `direction_derived` ya lo hace tras
la revisión. Extenderlo: todo campo derivado o estimado (dirección, retraso propagado,
rumbo) lleva su marca, y la UI tiene una regla explícita para cada nivel de confianza —
incluida «no mostrar».

**V2-5 · Ingesta en dos planos.** Plano de hechos (posiciones, retrasos) con cadencia de
1 minuto; plano de catálogo (estaciones, líneas, geometría) que cambia unas pocas veces
al año. Hoy están mezclados en `discover_data.main()`, que es justamente el problema de
composición que ambos revisores señalaron.

---

## 4. Paquetes de trabajo ejecutables

Cada uno es autónomo y verificable. Ejecutor sugerido según el tipo de trabajo:
**codex** para refactor mecánico y de superficie amplia; **grok** para lo que exige
razonar sobre el dominio y los casos límite.

| WP | Trabajo | Riesgo | Ejecutor | Criterio de aceptación |
|---|---|---|---|---|
| **WP-1** | Módulo `health` con invariantes de negocio y umbrales por franja horaria | R-1 | grok | Falla con feed vacío y con feed de núcleo desconocido; tests con reloj inyectado |
| **WP-2** | Sentido de marcha por vector de movimiento / `trip_headsign`, nunca lexicográfico | R-2 | grok | Sentido correcto en ≥95 % de una muestra etiquetada a mano; nunca se emite sentido sin marca de confianza |
| **WP-3** | Adaptador de estado externo (DynamoDB o Firestore) tras el puerto existente | R-3 | codex | Los tests de rumbo pasan contra el adaptador real con el proceso reiniciado entre lecturas |
| **WP-4** | Política de tiempo: UTC interno, `>24:00:00` en `stop_times`, cambios de hora | R-4 | grok | Tests con servicio nocturno y con los dos domingos de cambio horario de 2027 |
| **WP-5** | Conector de `trip_updates` + propagación de retraso aguas abajo, marcada como estimación | R-5 | grok | Contrato contra fixture real; toda estimación lleva `estimated=True` |
| **WP-6** | GTFS estático como artefacto versionado con arranque desde último bueno | R-6 | codex | El arranque funciona con las tres fuentes caídas si hay versión previa; falla si no la hay |
| **WP-7** | Catálogo de estaciones desde `stops.txt`, no desde lo observado | R-7 | codex | El catálogo cubre la red completa; lo observado sólo valida |
| **WP-8** | Parseo defensivo por entidad + traducción de todos los fallos de formato | R-8 | codex | Fixtures hostiles: `entity` no lista, `vehicle` array, timestamp no numérico, JSON raíz array |
| **WP-9** | Cliente protobuf como camino principal + test de equivalencia con JSON | R-9 | grok | Equivalencia campo a campo demostrada sobre captura simultánea |
| **WP-10** | Fixtures adversariales para toda la superficie de ingesta | R-10 | codex | Zip bomb sintético, XML bomb, respuesta sobredimensionada; cada uno con test rojo antes del fix |
| **WP-11** | Revisión legal: licencias, RGPD de suscripciones, moderación | R-11 | humano | No delegable a un ejecutor automático |
| **WP-12** | Separar `discover_data.main()` en pasos tipados (plano hechos / plano catálogo) | V2-5 | codex | Desaparece `# noqa: PLR0915` sin subir el umbral |

### Orden recomendado

1. **WP-1** primero. Sin invariantes, cualquier cosa que se rompa después se rompe en
   silencio, y esa es la lección más cara de la Fase 0.
2. **WP-8 y WP-10** a continuación: endurecen la ingesta antes de construir encima.
3. **WP-2 y WP-4**: los dos errores que el usuario *vería* y que destruirían la confianza.
4. **WP-3 y WP-6**: los que sólo se manifiestan al desplegar.
5. **WP-5 y WP-9**: ampliación de datos, ya sobre base firme.
6. **WP-11** en paralelo desde el principio, porque puede invalidar decisiones de producto.

---

## 5. Lo que este documento NO resuelve

- **Por qué** cambiaron los códigos de núcleo, y si volverán a cambiar. Sin eso, WP-1 es
  mitigación, no solución.
- Si `1080` fue correcto alguna vez o el Anexo A lo tomó de otra fuente.
- El acceso autenticado a NAP.
- El coste real de la difusión por Telegram durante una incidencia grande (C-8 fija
  cifras a validar, no validadas).
- Si `Tramificacion:TramosServicio` sirve para dibujar la línea con calidad suficiente.
