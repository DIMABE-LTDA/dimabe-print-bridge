import base64
import io

from PIL import Image

from bridge.escpos import looks_like_zpl, render_escpos

ESC, GS, LF = b"\x1b", b"\x1d", b"\n"


def texts(lines):
    return [l for l in lines if l["type"] == "text"]


def test_center_double_size():
    lines = render_escpos(ESC + b"@" + ESC + b"a\x01" + GS + b"!\x11" + b"MESA 12" + LF)
    line = texts(lines)[0]
    assert line == {"type": "text", "text": "MESA 12", "align": "center", "bold": False, "w": 2, "h": 2}


def test_bold_on_off():
    lines = render_escpos(ESC + b"E\x01" + b"2 x Lomo" + LF + ESC + b"E\x00" + b"+ medio" + LF)
    assert [l["bold"] for l in texts(lines)] == [True, False]


def test_esc_bang_sets_bold_and_double():
    line = texts(render_escpos(ESC + b"!\x38" + b"X" + LF))[0]
    assert (line["bold"], line["w"], line["h"]) == (True, 2, 2)


def test_cut_modes():
    assert {"type": "cut", "mode": "full"} in render_escpos(GS + b"V\x00")
    assert {"type": "cut", "mode": "partial"} in render_escpos(GS + b"V\x01")
    assert {"type": "cut", "mode": "partial"} in render_escpos(GS + b"VB\x03")


def test_feed_lines_produce_blank_lines():
    lines = render_escpos(b"a" + LF + LF + LF + ESC + b"d\x02")
    assert len(texts(lines)) == 5
    assert [l["text"] for l in texts(lines)][1:] == ["", "", "", ""]


def test_trailing_text_without_lf_is_kept():
    assert texts(render_escpos(b"sin salto"))[0]["text"] == "sin salto"


def test_raster_image():
    width_bytes, height = 1, 2
    data = GS + b"v0\x00" + bytes([width_bytes, 0, height, 0]) + b"\xff\x00"
    image = [l for l in render_escpos(data) if l["type"] == "image"][0]
    assert (image["width"], image["height"]) == (8, 2)
    png = Image.open(io.BytesIO(base64.b64decode(image["png_base64"])))
    assert png.size == (8, 2)
    assert png.getpixel((0, 0)) == 0 and png.getpixel((0, 1)) == 255


def test_unknown_command_grouped():
    lines = render_escpos(ESC + b"\x99\x98" + b"ok" + LF)
    assert lines[0] == {"type": "unknown", "hex": "1b9998"}
    assert texts(lines)[0]["text"] == "ok"


def test_ignored_commands_do_not_show():
    lines = render_escpos(ESC + b"M\x00" + ESC + b"t\x13" + ESC + b"p\x00\x19\xfa" + b"x" + LF)
    assert all(l["type"] != "unknown" for l in lines)


def test_cp858_text():
    assert texts(render_escpos(b"Ca\xa4a" + LF))[0]["text"] == "Caña"


def test_comanda_like_pos_has_no_unknown():
    comanda = (
        ESC + b"@" + ESC + b"M\x00" + ESC + b"a\x01" + GS + b"!\x11" + b"MESA 12" + LF
        + GS + b"!\x00" + b"Cocina caliente" + LF + ESC + b"a\x00" + b"-" * 48 + LF
        + ESC + b"E\x01" + b"2 x Lomo vetado" + LF + ESC + b"E\x00" + b"  + Punto medio" + LF
        + LF * 10 + GS + b"V\x00"
    )
    lines = render_escpos(comanda)
    assert not [l for l in lines if l["type"] == "unknown"]
    assert lines[-1] == {"type": "cut", "mode": "full"}
    assert texts(lines)[0]["text"] == "MESA 12"


def test_looks_like_zpl():
    assert looks_like_zpl(b"  ^XA^FO50,50^FDHola^FS^XZ")
    assert not looks_like_zpl(ESC + b"@hola")
