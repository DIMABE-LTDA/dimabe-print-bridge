"""Intérprete ESC/POS para la vista previa del modo prueba.

Convierte los bytes que mandó el POS en líneas que el panel dibuja como un
ticket. Cubre los comandos que usan el POS y las etiquetas; lo que no
entiende lo muestra como una marca discreta en vez de romper la vista.
"""
from __future__ import annotations

import base64
import io

from PIL import Image

ESC, GS, LF, CR, HT = 0x1B, 0x1D, 0x0A, 0x0D, 0x09
ALIGN = {0: "left", 48: "left", 1: "center", 49: "center", 2: "right", 50: "right"}
# Comandos ESC que no cambian la vista: (byte del comando, bytes de argumento)
ESC_IGNORED = {ord("M"): 1, ord("t"): 1, ord("p"): 3, ord("-"): 1, ord("2"): 0, ord("3"): 1, ord("R"): 1, ord("G"): 1}


def looks_like_zpl(data: bytes) -> bool:
    return b"^XA" in data.lstrip()[:64]


def _raster_png(width_bytes: int, height: int, bits: bytes) -> dict:
    width = width_bytes * 8
    image = Image.new("L", (width, height), 255)
    pixels = image.load()
    for y in range(height):
        for xb in range(width_bytes):
            index = y * width_bytes + xb
            byte = bits[index] if index < len(bits) else 0
            for bit in range(8):
                if byte & (0x80 >> bit):
                    pixels[xb * 8 + bit, y] = 0
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return {"type": "image", "png_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
            "width": width, "height": height}


class _Renderer:
    def __init__(self):
        self.lines: list[dict] = []
        self.buffer: list[str] = []
        self.line_style: dict | None = None
        self.unknown = bytearray()
        self.reset_style()

    def reset_style(self):
        self.style = {"align": "left", "bold": False, "w": 1, "h": 1}

    def flush_unknown(self):
        if self.unknown:
            self.lines.append({"type": "unknown", "hex": self.unknown.hex()})
            self.unknown = bytearray()

    def add_char(self, char: str):
        self.flush_unknown()
        if not self.buffer:
            self.line_style = dict(self.style)
        self.buffer.append(char)

    def newline(self):
        self.flush_unknown()
        style = self.line_style if self.buffer else self.style
        self.lines.append({"type": "text", "text": "".join(self.buffer), **dict(style)})
        self.buffer = []
        self.line_style = None

    def end(self):
        if self.buffer:
            self.newline()
        self.flush_unknown()


def render_escpos(data: bytes, columns: int = 48) -> list[dict]:
    r = _Renderer()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b == LF:
            r.newline()
            i += 1
        elif b == CR:
            i += 1
        elif b == HT:
            for _ in range(4):
                r.add_char(" ")
            i += 1
        elif b == ESC and i + 1 < n:
            cmd = data[i + 1]
            arg = data[i + 2] if i + 2 < n else 0
            if cmd == ord("@"):
                r.reset_style()
                i += 2
            elif cmd == ord("a"):
                r.style["align"] = ALIGN.get(arg, "left")
                i += 3
            elif cmd == ord("E"):
                r.style["bold"] = bool(arg & 1)
                i += 3
            elif cmd == ord("!"):
                r.style.update(bold=bool(arg & 0x08), h=2 if arg & 0x10 else 1, w=2 if arg & 0x20 else 1)
                i += 3
            elif cmd == ord("d"):
                for _ in range(max(arg, 1)):
                    r.newline()
                i += 3
            elif cmd in ESC_IGNORED:
                i += 2 + ESC_IGNORED[cmd]
            else:
                r.unknown += data[i:i + 2]
                i += 2
                while i < n and (data[i] < 0x20 or data[i] >= 0x80) and data[i] not in (LF, ESC, GS):
                    r.unknown.append(data[i])
                    i += 1
        elif b == GS and i + 1 < n:
            cmd = data[i + 1]
            arg = data[i + 2] if i + 2 < n else 0
            if cmd == ord("!"):
                r.style["w"] = min((arg >> 4) + 1, 8)
                r.style["h"] = min((arg & 0x0F) + 1, 8)
                i += 3
            elif cmd == ord("V"):
                if r.buffer:
                    r.newline()
                r.flush_unknown()
                if arg in (65, 66):
                    r.lines.append({"type": "cut", "mode": "full" if arg == 65 else "partial"})
                    i += 4
                else:
                    r.lines.append({"type": "cut", "mode": "partial" if arg in (1, 49) else "full"})
                    i += 3
            elif cmd == ord("v") and arg == ord("0") and i + 7 < n:
                width_bytes = data[i + 4] | (data[i + 5] << 8)
                height = data[i + 6] | (data[i + 7] << 8)
                start = i + 8
                end = start + width_bytes * height
                if r.buffer:
                    r.newline()
                r.flush_unknown()
                r.lines.append(_raster_png(width_bytes, height, data[start:end]))
                i = end
            else:
                r.unknown += data[i:i + 2]
                i += 2
        elif b < 0x20:
            r.unknown.append(b)
            i += 1
        else:
            r.add_char(bytes([b]).decode("cp858", errors="replace"))
            i += 1
    r.end()
    return r.lines
