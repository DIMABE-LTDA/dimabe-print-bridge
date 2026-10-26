import pytest

from bridge.backends import Target
from bridge.errors import BridgeError
from bridge.outbox import Outbox

IP = Target(kind="ip", ip="192.168.1.50", port=9100)
BACKUP = Target(kind="usb", printer_name="Cocina general")


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def box(tmp_path, clock):
    return Outbox(tmp_path / "cola.sqlite", clock=clock)


def test_add_list_and_count(box, clock):
    job = box.add("j1", IP, "kitchen", b"\x1b@x", fallback=BACKUP)
    clock.now += 30
    assert box.counts() == {"total": 1, "by_target": {"192.168.1.50:9100": 1}, "oldest_age_s": 30}
    pending = box.pending()
    assert pending[0]["id"] == job and pending[0]["job_id"] == "j1" and "data" not in pending[0]
    due = box.due()
    assert due[0].target == IP and due[0].fallback == BACKUP and due[0].data == b"\x1b@x"


def test_same_job_id_not_duplicated(box):
    first = box.add("j1", IP, "kitchen", b"a")
    assert box.add("j1", IP, "kitchen", b"a") == first
    assert box.counts()["total"] == 1
    assert box.is_pending("j1", IP.label)


def test_limit_per_target(tmp_path, clock):
    box = Outbox(tmp_path / "c.sqlite", clock=clock, max_per_target=2)
    box.add("a", IP, "kitchen", b"1")
    box.add("b", IP, "kitchen", b"2")
    with pytest.raises(BridgeError) as exc:
        box.add("c", IP, "kitchen", b"3")
    assert exc.value.code == "queue_full" and exc.value.status == 503


def test_backoff_and_expiry(box, clock):
    job = box.add("j1", IP, "kitchen", b"a")
    waits = []
    for _ in range(5):
        box.mark_failed(job, "sin conexión")
        item = next(j for j in box.pending() if j["id"] == job)
        waits.append(round(item["next_try"] - clock.now))
    assert waits == [10, 20, 40, 60, 60]
    assert box.due() == []
    clock.now += 60
    assert [j.id for j in box.due()] == [job]
    clock.now += 7200
    box.mark_failed(job, "sin conexión")
    assert box.counts()["total"] == 0
    assert box.expired()[0]["job_id"] == "j1"


def test_done_remembered_24h_and_persistent(tmp_path, clock):
    path = tmp_path / "cola.sqlite"
    box = Outbox(path, clock=clock)
    job = box.add("j1", IP, "kitchen", b"a")
    box.mark_done(job)
    assert box.counts()["total"] == 0
    reopened = Outbox(path, clock=clock)
    assert reopened.was_done("j1", IP.label, test_mode=False)
    assert not reopened.was_done("j1", IP.label, test_mode=True)
    reopened.remember_done("j2", "Boletas", test_mode=False)
    clock.now += 86401
    assert not reopened.was_done("j2", "Boletas", test_mode=False)


def test_pending_survives_restart(tmp_path, clock):
    path = tmp_path / "cola.sqlite"
    Outbox(path, clock=clock).add("j1", IP, "kitchen", b"a")
    assert [j.job_id for j in Outbox(path, clock=clock).due()] == ["j1"]


def test_discard_and_retry_now(box, clock):
    job = box.add("j1", IP, "kitchen", b"a")
    box.mark_failed(job, "x")
    assert box.due() == []
    box.retry_now(job)
    assert [j.id for j in box.due()] == [job]
    box.discard(job)
    assert box.counts()["total"] == 0


def test_corrupted_file_is_set_aside(tmp_path, clock):
    path = tmp_path / "cola.sqlite"
    path.write_bytes(b"esto no es una base sqlite" * 100)
    box = Outbox(path, clock=clock)
    assert box.corrupted_backup is not None and box.corrupted_backup.exists()
    box.add("j1", Target(kind="ip", ip="192.168.50.11", port=9100), "kitchen", b"x")
    assert box.counts()["total"] == 1
