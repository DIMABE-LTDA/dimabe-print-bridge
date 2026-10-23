import pytest

from bridge.backends import Target
from bridge.capture import CaptureStore, VirtualBackend
from bridge.config import Settings
from bridge.errors import BridgeError

USB = Target(kind="usb", printer_name="Boletas")
IP = Target(kind="ip", ip="192.168.1.50", port=9100)


def make(**settings):
    store = CaptureStore()
    s = Settings(test_mode=True, **settings)
    slept = []
    return store, VirtualBackend(store, lambda: s, sleep=slept.append), slept


def test_captures_raw_image_and_cashbox():
    store, backend, _ = make()
    backend.send_raw(IP, b"\x1b@comanda", "Dimabe kitchen")
    backend.print_image(USB, b"jpeg", 80, "Dimabe receipt")
    backend.open_cashbox(USB)
    jobs = store.list()
    assert [j.kind for j in jobs] == ["cashbox", "image", "raw"]
    assert jobs[2].target_label == "192.168.1.50:9100"
    assert jobs[2].document_type == "kitchen"
    assert store.targets() == {"192.168.1.50:9100": 1, "Boletas": 2}
    assert store.get(jobs[2].id).data == b"\x1b@comanda"
    assert [j.kind for j in store.list("Boletas")] == ["cashbox", "image"]


def test_virtual_accepts_image_to_ip():
    store, backend, _ = make()
    backend.print_image(IP, b"jpeg", 80, "job")
    assert store.list()[0].kind == "image"


def test_store_limit_and_clear():
    store = CaptureStore(max_items=3)
    for i in range(5):
        store.add("x", "receipt", "raw", bytes([i]))
    assert [j.data for j in store.list()] == [b"\x04", b"\x03", b"\x02"]
    store.clear()
    assert store.list() == []


@pytest.mark.parametrize("mode,text", [("no_paper", "Sin papel (simulado)."), ("offline", "Impresora desconectada (simulado).")])
def test_simulated_failures(mode, text):
    store, backend, _ = make(test_response=mode)
    with pytest.raises(BridgeError) as exc:
        backend.send_raw(USB, b"x", "job")
    assert exc.value.message == text
    assert store.list() == []


def test_timeout_sleeps_then_fails():
    _, backend, slept = make(test_response="timeout", test_delay_s=5)
    with pytest.raises(BridgeError) as exc:
        backend.send_raw(USB, b"x", "job")
    assert exc.value.message == "Tiempo de espera agotado (simulado)."
    assert slept == [5]


def test_delay_applies_on_success():
    _, backend, slept = make(test_delay_s=1)
    backend.send_raw(USB, b"x", "job")
    assert slept == [1]


def test_one_in_three_fails_exactly_every_third():
    _, backend, _ = make(test_response="one_in_three")
    results = []
    for _ in range(6):
        try:
            backend.send_raw(USB, b"x", "job")
            results.append("ok")
        except BridgeError:
            results.append("fail")
    assert results == ["ok", "ok", "fail", "ok", "ok", "fail"]


def test_virtual_lists_seen_targets():
    _, backend, _ = make()
    backend.send_raw(USB, b"x", "job")
    assert backend.list_printers() == [{"Name": "Boletas", "DriverName": "Virtual", "PortName": "virtual://Boletas", "Type": "Virtual", "Shared": False}]
    assert backend.queue_status("Boletas") == "ok"
