import pytest

from bridge.backends import Target
from bridge.config import Settings
from bridge.journal import Journal
from bridge.outbox import Outbox
from bridge.outbox_worker import OutboxWorker
from bridge.server import build_context, create_local_app
from bridge.service import PrintService
from tests.test_resilience_service import RoutedBackend

IP = Target(kind="ip", ip="192.168.1.50", port=9100)
BACKUP = Target(kind="ip", ip="192.168.1.60", port=9100)
HOST = {"Host": "127.0.0.1:3210"}


@pytest.fixture
def env(tmp_path):
    settings = Settings()
    journal = Journal(tmp_path / "j.sqlite")
    journal.start()
    outbox = Outbox(tmp_path / "cola.sqlite")
    yield settings, journal, outbox, tmp_path
    journal.stop()


def test_worker_prints_due_jobs(env):
    settings, journal, outbox, _ = env
    real = RoutedBackend()
    service = PrintService(lambda: settings, real, RoutedBackend(), journal, outbox=outbox)
    outbox.add("c-1", IP, "kitchen", b"\x1b@A")
    OutboxWorker(outbox, service, lambda: settings).run_once()
    assert real.calls == [("192.168.1.50:9100", b"\x1b@A")]
    assert outbox.counts()["total"] == 0
    assert outbox.was_done("c-1", IP.label, test_mode=False)


def test_worker_reschedules_on_failure(env):
    settings, journal, outbox, _ = env
    real = RoutedBackend(down={IP.label})
    service = PrintService(lambda: settings, real, RoutedBackend(), journal, outbox=outbox)
    job = outbox.add("c-1", IP, "kitchen", b"x")
    OutboxWorker(outbox, service, lambda: settings).run_once()
    pending = outbox.pending()[0]
    assert pending["id"] == job and pending["attempts"] == 1 and "192.168.1.50" in pending["last_error"]


def test_worker_uses_backup_when_primary_still_down(env):
    settings, journal, outbox, _ = env
    real = RoutedBackend(down={IP.label})
    service = PrintService(lambda: settings, real, RoutedBackend(), journal, outbox=outbox)
    outbox.add("c-1", IP, "kitchen", b"x", fallback=BACKUP)
    OutboxWorker(outbox, service, lambda: settings).run_once()
    assert real.calls[0][0] == BACKUP.label
    assert outbox.counts()["total"] == 0


def test_pending_job_survives_restart(env):
    settings, journal, outbox, tmp_path = env
    outbox.add("c-1", IP, "kitchen", b"x")
    reopened = Outbox(tmp_path / "cola.sqlite")
    real = RoutedBackend()
    service = PrintService(lambda: settings, real, RoutedBackend(), journal, outbox=reopened)
    OutboxWorker(reopened, service, lambda: settings).run_once()
    assert len(real.calls) == 1


def test_panel_queue_endpoints_and_attention(data_dir):
    ctx = build_context(real=RoutedBackend(down={IP.label}))
    try:
        client = create_local_app(ctx).test_client()
        ok = client.post("/api/print", json={"ip_address": "192.168.1.50", "raw_hex": "1B40",
                                             "document_type": "kitchen", "job_id": "c-9"}, headers=HOST)
        assert ok.get_json()["queued"] is True
        queue = client.get("/panel/api/queue", headers=HOST).get_json()
        assert queue["counts"]["total"] == 1 and queue["pending"][0]["job_id"] == "c-9"
        status = client.get("/panel/api/status", headers=HOST).get_json()
        assert any("en cola" in a["title"] for a in status["attention"])
        headers = {**HOST, "X-CSRF-Token": ctx.csrf_token, "Origin": "http://127.0.0.1:3210"}
        job_id = queue["pending"][0]["id"]
        assert client.post(f"/panel/api/queue/{job_id}/retry", headers=headers).status_code == 200
        assert client.post(f"/panel/api/queue/{job_id}/discard", headers=headers).status_code == 200
        assert client.get("/panel/api/queue", headers=HOST).get_json()["counts"]["total"] == 0
    finally:
        ctx.journal.stop()
