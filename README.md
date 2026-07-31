# Cercanías Madrid — Plataforma de información en tiempo real

Sistema de información en tiempo real para la red de Cercanías de Madrid, construido
sobre el feed GTFS-RT público de Renfe y la información de infraestructura de Adif.

> **Estado: Fase 0 — descubrimiento.** Esta fase no entrega producto. Su único objetivo
> es verificar que los datos son lo que se cree que son, antes de construir nada encima.
> No hay API, ni frontend, ni despliegue.

## Atribución

> Datos de horarios y tiempo real: Renfe (CC BY 4.0). Información de infraestructura y
> estado de la red: Adif. Proyecto independiente, sin relación con Renfe ni Adif; no vende
> billetes ni tramita reclamaciones.

Ver [`NOTICE`](NOTICE) para el detalle de licencias de cada fuente de datos.

## Arquitectura

Puertos y adaptadores. El dominio no importa nada de AWS, GCP ni Telegram.

```
packages/domain/              Modelos, filtro de núcleo, parser de andén, rumbo, matching
packages/connectors-renfe/    GTFS estático (cascada de 3 orígenes) y cliente GTFS-RT
packages/connectors-adif/     Explorador IDEADIF y correlación de estaciones
packages/persistence/         Repositorio de estado (current + previous por vehicle.id)
apps/worker/                  Puntos de entrada de descubrimiento
fixtures/                     Entradas reales para los tests de contrato
docs/informes/                Salidas de descubrimiento (generadas)
docs/adr/                     Decisiones de arquitectura
```

## Requisitos

- Python 3.13 (fijado; no usar 3.14)
- [`uv`](https://docs.astral.sh/uv/)
- `make`

## Uso

```bash
make install               # uv sync --all-packages
make test                  # tests de contrato, sin red
make lint                  # ruff
make typecheck             # mypy strict
make ci                    # lint + typecheck + test
```

Objetivos de descubrimiento (requieren red):

```bash
make check-feed-freshness  # E2 — mide el desfase real de header.timestamp
make discover-data         # E7 — catálogo de estaciones y cobertura de trip_id
make ideadif-explore       # E6 — GetCapabilities de WMS/WFS de IDEADIF
```

## Jerarquía de fuentes de verdad

No negociable, en este orden:

1. **`ANEXO-A-Hechos-Verificados-Feed.md`** — comportamiento real del feed, verificado con
   captura en vivo. Prevalece sobre la especificación, sobre el estándar GTFS-RT y sobre
   cualquier conocimiento previo. Si el Anexo A dice que un campo no existe, no existe.
2. **`CORRECCIONES-Spec-v1.1.md`** — correcciones C-1 a C-12.
3. **`Especificacion-Tecnica-Integral-v1.0.md`** — especificación base.

### Consecuencias prácticas

Tres reglas que el feed impone y que contradicen lo que cabría suponer:

- **`bearing` y `speed` no existen** en el feed (A.5). El rumbo se calcula comparando con
  el snapshot anterior, con umbral mínimo de desplazamiento de 25 m.
- **`route_id` nunca está presente** (A.4). Siempre se deriva, con fallback marcado.
- **El andén viaja dentro de `vehicle.vehicle.label`** como `str | None` (A.6), no como
  entero. `PLATF.(0)` es `"0"`, no `None`; `PLATF.(8AM)` es válido.

El filtro del núcleo de Madrid es prefijo `1080` en `tripId` **más** el patrón
`SPECIAL_10_*` (A.2). Filtrar por etiqueta de línea produce datos incorrectos en silencio.

## Licencia

Código: [Apache License 2.0](LICENSE).

La licencia Apache cubre únicamente el código. Los datos de Renfe, Adif y CRTM mantienen
sus propios términos — ver [`NOTICE`](NOTICE).
