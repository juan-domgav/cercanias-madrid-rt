"""La app web existe en dos sitios: apps/web/index.html (lo que se sube al
bucket, ver DESPLIEGUE.md) y apps/web/public/index.html (raiz de Firebase).
Si divergen, un arreglo llega a una sola de las dos."""

from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"


def test_las_dos_copias_de_index_html_son_identicas() -> None:
    fuente = (WEB / "index.html").read_bytes()
    publica = (WEB / "public" / "index.html").read_bytes()
    assert fuente == publica, "copia apps/web/index.html a apps/web/public/index.html"


def test_la_web_pinta_el_estado_de_la_funcion_compartida() -> None:
    """RB-008: la web no tiene su propia cascada de etiquetas: pinta ``display``,
    que la ingesta rellena con ``board_status.departure_status`` (la misma que el bot)."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "d.display" in html
    assert "s.label" in html and "s.notices" in html and "s.approximate" in html
    assert ".delay.estimated" in html, "el tono 'estimated' sigue teniendo estilo"
    # Las cascadas antiguas desaparecen: eran las que divergian del bot.
    assert '"sin confirmar"' not in html
    assert "d.inbound_delay" not in html
    assert 'd.delay_source === "inbound_estimate"' not in html


def test_el_fallback_de_la_web_nunca_contradice_el_dato_en_vivo() -> None:
    """Con un boards.json previo a RB-008 (sin ``display``) la web solo pinta el
    dato en vivo: ningun aviso de entrante ni "sin confirmar"."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    fallback = html[html.index("const s = d.display ||"):html.index("const cls =")]
    assert "notices: []" in fallback
    assert "inbound" not in fallback
