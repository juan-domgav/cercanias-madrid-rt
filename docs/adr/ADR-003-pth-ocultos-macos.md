# ADR-003 — Paquetes del workspace no importables en macOS: `.pth` ocultos

- **Estado:** Aceptada
- **Fecha:** 2026-07-31
- **Ámbito:** entorno de desarrollo local (macOS). No afecta a Linux ni a CI.

## Síntoma

Los cinco paquetes del workspace se instalan sin error (`uv sync --all-packages`
los reporta instalados, `uv pip list` los muestra, existe su `dist-info`), pero
cualquier importación falla:

```
ModuleNotFoundError: No module named 'cercanias_domain'
```

El fallo es **intermitente de forma engañosa**: justo después de `make install`
todo funciona y los 112 tests pasan; en la siguiente invocación de cualquier
target, vuelve a romperse. Esto hace que parezca un problema de código o de
tests inestables cuando no lo es.

## Causa

Tres hechos que sólo son destructivos combinados:

1. `uv` instala los paquetes editables mediante ficheros
   `_editable_impl_<paquete>.pth` en `site-packages`, cuyo contenido es la ruta
   absoluta al `src/` correspondiente.
2. `uv` crea esos ficheros con el flag **`UF_HIDDEN`** de macOS activado.
3. **CPython 3.13** incorporó en `site.py` una comprobación que **omite en
   silencio** cualquier `.pth` marcado como oculto.

El resultado es que la ruta nunca llega a `sys.path`. No se emite ningún aviso,
ningún error y ningún log: el paquete simplemente no existe para el intérprete.

Comprobación:

```console
$ ls -lO .venv/lib/python3.13/site-packages/_editable_impl_cercanias_domain.pth
... hidden ...
$ chflags nohidden .venv/lib/python3.13/site-packages/_editable_impl_*.pth
$ uv run python -c "import cercanias_domain; print('OK')"
OK
```

Se descartaron por medición, no por intuición:

- **Codificación local**: la ruta contenía un carácter no ASCII. `locale` ya era
  UTF-8 y forzar `LANG`/`PYTHONUTF8=1` no cambiaba nada.
- **Ruta con espacio final**: el proyecto vivía bajo `App Cercanías /`. Se
  renombró el directorio y el fallo persistió. La ruta no era la causa.
- **Configuración del workspace**: `pyproject.toml` declara correctamente los
  cinco miembros y `os.path.exists()` sobre el contenido del `.pth` devuelve
  `True`.

## Decisión

1. Limpiar el flag con `chflags nohidden` sobre los `_editable_impl_*.pth`.
2. **Desactivar la sincronización automática de `uv run`** mediante
   `export UV_NO_SYNC := 1` en el `Makefile`. Sin esto la corrección no
   sobrevive: cada `uv run` re-sincroniza el entorno y vuelve a marcar los
   ficheros como ocultos *antes* de ejecutar el comando, de modo que limpiar el
   flag únicamente en `install` no sirve de nada.
3. Declarar `unhide` como prerequisito de todos los targets que ejecutan código.
4. `make install` es el único punto que sincroniza, y lo hace forzando
   `UV_NO_SYNC=0`.

## Consecuencias

- Tras cambiar dependencias hay que ejecutar `make install` explícitamente; ya
  no basta con que `uv run` sincronice solo. Es un coste aceptable a cambio de
  un entorno determinista.
- En Linux el problema no se da: `UF_HIDDEN` no existe y `chflags` tampoco. El
  target `unhide` termina en no-op silencioso gracias a `2>/dev/null || true`,
  por lo que **CI no necesita tratamiento especial**.
- Si una versión futura de `uv` deja de marcar los `.pth` como ocultos, o si
  CPython revierte la comprobación de `site.py`, este ADR puede retirarse junto
  con el target `unhide` y la variable `UV_NO_SYNC`.

## Nota

Este fallo consumió una parte desproporcionada de la Fase 0 precisamente por su
silencio: un entorno que se instala "correctamente" y luego no importa nada, con
tests que pasan una vez y fallan a la siguiente, se parece mucho a un problema de
código. Se documenta aquí para que nadie vuelva a diagnosticarlo desde cero.
