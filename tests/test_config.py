import json

from bridge import __version__
from bridge.config import (
    Settings,
    apply_installer_seed,
    data_dir as get_data_dir,
    load_settings,
    save_settings,
    settings_path,
)

DEFAULT_ORIGINS = [
    "https://*.odoo.com",
    "https://*.grupoloslirios.cl",
    "http://*.localhost",
    "http://*.localhost:*",
    "http://localhost",
    "http://localhost:*",
    "http://127.0.0.1",
    "http://127.0.0.1:*",
]


def test_version():
    assert __version__ == "1.1.0"


def test_data_dir_honors_env(data_dir):
    assert get_data_dir() == data_dir
    assert settings_path() == data_dir / "settings.json"


def test_defaults_match_spec(data_dir):
    s = load_settings()
    assert s.port == 3210
    assert s.retention_days == 90
    assert s.allowed_printer_ports == list(range(9100, 9110))
    assert s.allowed_origins == DEFAULT_ORIGINS
    assert s.test_mode is False
    assert s.remote_panel_enabled is False
    assert s.remote_panel_port == 3211
    assert s.store_payload is True
    assert s.store_images is False


def test_roundtrip_utf8(data_dir):
    s = Settings(api_key="ñandú", test_mode=True)
    save_settings(s)
    loaded = load_settings()
    assert loaded.api_key == "ñandú"
    assert loaded.test_mode is True


def test_corrupt_json_returns_defaults(data_dir):
    settings_path().write_text("{no es json", encoding="utf-8")
    assert load_settings() == Settings()


def test_unknown_keys_ignored(data_dir):
    settings_path().write_text(json.dumps({"port": 3300, "zzz": 1}), encoding="utf-8")
    assert load_settings().port == 3300


def test_out_of_range_retention_resets(data_dir):
    for value in (0, 99999):
        settings_path().write_text(json.dumps({"retention_days": value}), encoding="utf-8")
        assert load_settings().retention_days == 90


def test_invalid_test_response_resets(data_dir):
    settings_path().write_text(json.dumps({"test_response": "boom", "test_delay_s": 7}), encoding="utf-8")
    s = load_settings()
    assert s.test_response == "ok"
    assert s.test_delay_s == 0


def test_seed_applied_once(data_dir):
    seed = data_dir / "installer-seed.json"
    seed.write_text(json.dumps({"port": 3300, "api_key": "clave", "remote_panel_enabled": True}), encoding="utf-8")
    s = apply_installer_seed(Settings(), seed)
    assert (s.port, s.api_key, s.remote_panel_enabled, s.installer_seed_applied) == (3300, "clave", True, True)
    assert not seed.exists()
    seed.write_text(json.dumps({"port": 4000}), encoding="utf-8")
    again = apply_installer_seed(s, seed)
    assert again.port == 3300
    assert not seed.exists()
