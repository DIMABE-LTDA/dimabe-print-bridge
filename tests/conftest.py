import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Apunta la carpeta de datos del puente a un directorio temporal."""
    monkeypatch.setenv("DIMABE_BRIDGE_DATA_DIR", str(tmp_path))
    return tmp_path
