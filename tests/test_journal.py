from datetime import date, datetime, timedelta

import pytest

from bridge.journal import Journal


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 23, 13, 41, 8))


@pytest.fixture
def journal(tmp_path, clock):
    j = Journal(tmp_path / "bitacora.sqlite", clock=clock)
    j.start()
    yield j
    j.stop()


def _print(j, **extra):
    fields = dict(kind="print", document_type="boleta", target_label="Boletas", target_kind="usb",
                  origin="https://x.odoo.com", client_ip="127.0.0.1", bytes=10, duration_ms=5,
                  result="ok", http_status=200, message="Printed on 'Boletas'.", test_mode=0,
                  payload_kind="raw", payload=b"\x1b@hola")
    fields.update(extra)
    j.record(**fields)


def test_record_and_query_day(journal):
    _print(journal)
    journal.flush()
    rows = journal.query_day(date(2026, 10, 23))
    assert len(rows) == 1
    assert rows[0]["target_label"] == "Boletas"
    assert "payload" not in rows[0]
    assert rows[0]["day"] == "2026-10-23"
    assert journal.query_day(date(2026, 10, 22)) == []


def test_get_includes_payload_hex(journal):
    _print(journal)
    journal.flush()
    event_id = journal.query_day(date(2026, 10, 23))[0]["id"]
    assert journal.get(event_id)["payload"] == "1b40686f6c61"
    assert journal.get(99999) is None


def test_filters_result_target_text(journal, clock):
    _print(journal)
    clock.now += timedelta(minutes=1)
    _print(journal, target_label="Cocina caliente", result="error", message="timed out")
    clock.now += timedelta(minutes=1)
    _print(journal, kind="rejected", result="rejected", target_label="", message="Origin not allowed")
    journal.flush()
    day = date(2026, 10, 23)
    assert [r["result"] for r in journal.query_day(day)] == ["rejected", "error", "ok"]
    assert len(journal.query_day(day, result="error")) == 1
    assert len(journal.query_day(day, target="Cocina caliente")) == 1
    assert len(journal.query_day(day, text="timed")) == 1
    assert len(journal.query_day(day, document_type="boleta")) == 3
    assert len(journal.query_day(day, limit=1, offset=1)) == 1


def test_summary_hours_and_targets(journal, clock):
    _print(journal)
    _print(journal, target_label="Cocina caliente", result="error", message="timed out")
    clock.now = clock.now.replace(hour=9)
    _print(journal)
    _print(journal, kind="rejected", result="rejected", target_label="")
    journal.flush()
    s = journal.summary_day(date(2026, 10, 23))
    assert (s["total"], s["ok"], s["error"], s["rejected"]) == (4, 2, 1, 1)
    assert s["by_hour"][13] == 1 and s["by_hour"][9] == 1
    assert s["errors_by_hour"][13] == 1
    assert s["by_target"]["Boletas"]["ok"] == 2
    assert s["by_target"]["Cocina caliente"]["error"] == 1
    assert s["by_target"]["Cocina caliente"]["last_message"] == "timed out"


def test_purge_respects_retention(journal, clock):
    real_now = clock.now
    clock.now = real_now - timedelta(days=100)
    _print(journal)
    clock.now = real_now - timedelta(days=10)
    _print(journal)
    clock.now = real_now
    journal.flush()
    assert journal.purge(90) == 1
    assert journal.size_info()["days"] == 1


def test_export_csv_has_bom_and_header(journal):
    _print(journal, target_label="Cocina Ñandú")
    journal.flush()
    csv_text = journal.export_csv(date(2026, 10, 23))
    assert csv_text.startswith("﻿")
    header, row = csv_text.lstrip("﻿").strip().splitlines()
    assert header.startswith("id;hora;")
    assert "Cocina Ñandú" in row
    assert "payload" not in header


def test_health_throttled_per_minute(journal, clock):
    assert journal.should_log_health("https://a.odoo.com")
    assert not journal.should_log_health("https://a.odoo.com")
    assert journal.should_log_health("https://b.odoo.com")
    clock.now += timedelta(seconds=61)
    assert journal.should_log_health("https://a.odoo.com")


def test_record_never_raises_when_db_broken(tmp_path, clock):
    broken = tmp_path / "es_directorio.sqlite"
    broken.mkdir()
    j = Journal(broken, clock=clock)
    j.start()
    _print(j)
    j.flush()
    assert j.query_day(date(2026, 10, 23)) == []
    j.stop()
