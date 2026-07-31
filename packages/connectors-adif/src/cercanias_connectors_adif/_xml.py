"""Utilidades XML internas compartidas por los adaptadores de IDEADIF."""

from __future__ import annotations

import re
from collections.abc import Iterator
from xml.etree import ElementTree as ET

_NAMESPACE_STRIP_RE = re.compile(r"\{[^}]*\}")


def localname(tag: str) -> str:
    """Devuelve un tag XML sin su namespace de ElementTree."""
    return _NAMESPACE_STRIP_RE.sub("", tag)


def iter_descendants_named(root: ET.Element, name: str) -> Iterator[ET.Element]:
    """Itera descendientes cuyo nombre local coincide con ``name``."""
    for element in root.iter():
        if localname(element.tag) == name:
            yield element


def iter_children_named(parent: ET.Element, name: str) -> Iterator[ET.Element]:
    """Itera hijos cuyo nombre local coincide con ``name``."""
    for child in parent:
        if localname(child.tag) == name:
            yield child


def iter_member_features(root: ET.Element, feature_name: str) -> Iterator[ET.Element]:
    """Itera features GML anidadas bajo los miembros de una respuesta WFS."""
    for member in root:
        for feature in member:
            if localname(feature.tag) == feature_name:
                yield feature
