import hashlib
from datetime import date, datetime

import pytest

from bridge.config import Settings
from bridge.journal import Journal
from bridge.updater import Updater, compare_versions

PIN = "671F9489819782DD56B2BDE588BEEC030BF41018"
INSTALLER = b"MZ nuevo instalador"


def release(version="1.1.0.7", digest=True):
    asset = {"name": f"DimabePrintBridge-Setup-{version}.exe", "browser_download_url": "https://x/setup.exe",
             "size": len(INSTALLER)}
    if digest:
        asset["digest"] = "sha256:" + hashlib.sha256(INSTALLER).hexdigest()
    return {"tag_name": f"v{version}", "assets": [asset]}


class Harness:
    def __init__(self, tmp_path, mode="auto", now=datetime(2026, 10, 24, 3, 30), rel=None,
                 thumb=PIN, payload=INSTALLER, current="1.1.0.5"):
        self.settings = Settings(update_mode=mode)
        self.journal = Journal(tmp_path / "j.sqlite", clock=lambda: self.now)
        self.journal.start()
        self.now = now
        self.installed = []
        self.updater = Updater(
            lambda: self.settings, self.journal, current=current, pinned=PIN, updates_dir=tmp_path / "updates",
            fetch=lambda repo: rel if rel is not None else release(),
            download=lambda url, path: path.write_bytes(payload),
            verify=lambda path: (thumb, "UnknownError"),
            run_installer=lambda path, version: self.installed.append((path.name, version)),
            clock=lambda: self.now,
        )

    def close(self):
        self.journal.stop()


@pytest.fixture
def make(tmp_path):
    made = []

    def factory(**kwargs):
        h = Harness(tmp_path, **kwargs)
        made.append(h)
        return h

    yield factory
    for h in made:
        h.close()


def test_compare_versions():
    assert compare_versions("1.1.0.10", "1.1.0.9") > 0
    assert compare_versions("1.0.0", "1.0.0.0") == 0
    assert compare_versions("1.0.9", "1.1.0") < 0


def test_same_version_nothing(make):
    h = make(rel=release("1.1.0.5"))
    assert h.updater.check()["available"] is False
    assert h.updater.maybe_install() == "skipped:up_to_date"


def test_notify_mode_does_not_install(make):
    h = make(mode="notify")
    state = h.updater.check()
    assert state["available"] is True and state["latest"] == "1.1.0.7"
    assert h.updater.maybe_install() == "skipped:notify"
    assert h.installed == []


def test_off_mode_does_not_check(make):
    h = make(mode="off")
    assert h.updater.check()["available"] is False


def test_outside_window(make):
    h = make(now=datetime(2026, 10, 24, 14, 0))
    h.updater.check()
    assert h.updater.maybe_install() == "skipped:outside_window"


def test_recent_printing_blocks(make):
    h = make()
    h.journal.record(kind="print", result="ok", message="m")
    h.journal.flush()
    h.updater.check()
    assert h.updater.maybe_install() == "skipped:recent_activity"


def test_wrong_signature_rejected(make):
    h = make(thumb="0000")
    h.updater.check()
    assert h.updater.maybe_install() == "rejected:signature"
    assert h.installed == []
    h.journal.flush()
    rows = h.journal.query_day(date(2026, 10, 24))
    assert any(r["kind"] == "update" and r["result"] == "error" for r in rows)


def test_wrong_hash_rejected(make):
    h = make(payload=b"MZ otro archivo")
    h.updater.check()
    assert h.updater.maybe_install() == "rejected:hash"
    assert h.installed == []


def test_valid_update_installs(make):
    h = make()
    h.updater.check()
    assert h.updater.maybe_install() == "installed"
    assert h.installed == [("DimabePrintBridge-Setup-1.1.0.7.exe", "1.1.0.7")]
