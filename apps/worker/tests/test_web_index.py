"""La app web existe en dos sitios: apps/web/index.html (lo que se sube al
bucket, ver DESPLIEGUE.md) y apps/web/public/index.html (raiz de Firebase).
Si divergen, un arreglo llega a una sola de las dos."""

from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"


def test_las_dos_copias_de_index_html_son_identicas() -> None:
    fuente = (WEB / "index.html").read_bytes()
    publica = (WEB / "public" / "index.html").read_bytes()
    assert fuente == publica, "copia apps/web/index.html a apps/web/public/index.html"
