import hashlib

import pytest

from sleeptalk.qr import qr_matrix, qr_svg

LINK = "https://call.sleep.talk/#/call/r7Kp2QxV9mTzLw4N?t=Hq8sYd3FbJ6cXe1RuA5kGn0PvW2iZo7M"


def _bits(matrix: list[list[bool]]) -> str:
    return "".join("1" if dark else "0" for row in matrix for dark in row)


def test_a_call_link_encodes_to_the_symbol_a_phone_scanned() -> None:
    # Pinned to output that macOS's QR detector (CoreImage) read back as LINK;
    # every version from 1 to 40, filled to capacity, read back the same way.
    matrix = qr_matrix(LINK)
    assert len(matrix) == 37  # version 5
    assert hashlib.sha256(_bits(matrix).encode()).hexdigest() == (
        "232542a0be90d6440551038fa50cc7663f589cd44ab6c191dc20cad1d517b959")


def test_the_size_grows_with_the_text_and_has_finder_patterns() -> None:
    assert len(qr_matrix("a")) == 21
    assert len(qr_matrix("x" * 300)) > len(qr_matrix("x" * 100))
    matrix = qr_matrix(LINK)
    ring = [matrix[0][i] for i in range(7)] + [matrix[6][i] for i in range(7)]
    assert all(ring) and matrix[1][1] is False and matrix[3][3] is True
    with pytest.raises(ValueError):
        qr_matrix("x" * 3000)


def test_the_svg_is_dark_on_white_with_a_border() -> None:
    svg = qr_svg(LINK)
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45"')
    assert 'fill="#fff"' in svg and svg.count("h1v1h-1z") == _bits(qr_matrix(LINK)).count("1")
    assert "M4,4h1v1h-1z" in svg  # the first finder's corner, past the 4-module border
