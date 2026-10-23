"""Trabajos de ejemplo para probar sin POS (mismos comandos que manda el POS)."""
from __future__ import annotations

from datetime import datetime

ESC, GS, LF = b"\x1b", b"\x1d", b"\n"
COLUMNS = 48


def _t(text: str) -> bytes:
    return text.encode("cp858", errors="replace")


def _row(left: str, right: str) -> bytes:
    return _t(left + " " * max(1, COLUMNS - len(left) - len(right)) + right) + LF


def comanda_bytes() -> bytes:
    now = datetime.now().strftime("%H:%M")
    return (
        ESC + b"@" + ESC + b"M\x00" + ESC + b"a\x01" + GS + b"!\x11" + _t("MESA 12") + LF
        + GS + b"!\x00" + _t("Cocina caliente") + LF + ESC + b"a\x00" + _t("-" * COLUMNS) + LF
        + _row("Mesero: Camila R.", now) + _row("Comensales: 4", "#0231") + _t("-" * COLUMNS) + LF
        + ESC + b"E\x01" + _t("2 x Lomo vetado") + LF + ESC + b"E\x00" + _t("  + Punto medio") + LF
        + ESC + b"E\x01" + _t("1 x Pollo al jugo") + LF + _t("1 x Merluza austral") + LF
        + ESC + b"E\x00" + _t("  Nota: sin sal") + LF + _t("-" * COLUMNS) + LF
        + _row("4 productos", "NUEVO") + LF * 4 + GS + b"V\x00"
    )


def boleta_bytes() -> bytes:
    now = datetime.now().strftime("%d-%m-%Y %H:%M")
    return (
        ESC + b"@" + ESC + b"a\x01" + ESC + b"E\x01" + _t("RESTAURANT LOS LIRIOS") + LF + ESC + b"E\x00"
        + _t("BOLETA ELECTRONICA N 000231") + LF + _t(now) + LF + ESC + b"a\x00" + _t("-" * COLUMNS) + LF
        + _row("2 x Lomo vetado", "$31.800") + _row("1 x Pollo al jugo", "$12.900")
        + _row("1 x Merluza austral", "$14.500") + _t("-" * COLUMNS) + LF
        + ESC + b"E\x01" + _row("TOTAL", "$59.200") + ESC + b"E\x00"
        + ESC + b"a\x01" + _t("Boleta de ejemplo del modo prueba") + LF + LF * 4 + GS + b"V\x00"
    )


def etiqueta_zpl() -> bytes:
    return (b"^XA^CI28^FO40,30^A0N,40,40^FDLOTE L-2026-881^FS"
            b"^FO40,90^A0N,30,30^FDPollo entero 2,1 kg^FS^FO40,140^BCN,80,Y,N,N^FD7801234567890^FS^XZ")


def test_ticket(label: str) -> bytes:
    now = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    return (
        ESC + b"@" + ESC + b"a\x01" + GS + b"!\x11" + _t("PRUEBA") + LF + GS + b"!\x00"
        + _t("Dimabe Print Bridge") + LF + _t(label[:COLUMNS]) + LF + _t(now) + LF
        + _t("Si lees esto, la impresora funciona.") + LF + LF * 4 + GS + b"V\x00"
    )


SAMPLES = {
    "boleta": ("receipt", boleta_bytes),
    "comanda": ("kitchen", comanda_bytes),
    "etiqueta": ("label", etiqueta_zpl),
}
