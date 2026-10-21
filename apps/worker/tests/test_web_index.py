"""La app web existe en dos sitios: apps/web/index.html (lo que se sube al
bucket, ver DESPLIEGUE.md) y apps/web/public/index.html (raiz de Firebase).
Si divergen, un arreglo llega a una sola de las dos."""

from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"


def test_las_dos_copias_de_index_html_son_identicas() -> None:
    fuente = (WEB / "index.html").read_bytes()
    publica = (WEB / "public" / "index.html").read_bytes()
    assert fuente == publica, "copia apps/web/index.html a apps/web/public/index.html"


def test_la_web_pinta_el_estimado_como_estimado() -> None:
    """RB-007: el retraso estimado desde el entrante no se pinta como dato en vivo."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'd.delay_source === "inbound_estimate"' in html
    assert "(estimado)" in html
    assert ".delay.estimated" in html
