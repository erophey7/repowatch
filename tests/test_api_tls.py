"""Validation of the optional TLS setup without opening any sockets."""

import pytest
import yaml

from repowatch.errors import ConfigError
from repowatch.config.load import load_config


def _config_path(tmp_path, **fields):
    path = tmp_path / "config.yaml"
    raw = {
        "state_db": str(tmp_path / "state.sqlite3"),
        "cache_base_url": "http://localhost:8080",
        "repos": [{"id": "r", "type": "apk", "arch": "x86_64", "upstream": "https://example.org/alpine"}],
        **fields,
    }
    path.write_text(yaml.safe_dump(raw))
    return path


@pytest.mark.parametrize("settings", [
    {"tls_cert_path": "cert.pem"}, {"tls_key_path": "key.pem"},
    {"tls_cert_path": "", "tls_key_path": "key.pem"},
    {"tls_cert_path": "cert.pem", "tls_key_path": 123},
    {"tls_cert_path": "", "tls_key_path": ""},
])
def test_tls_requires_two_nonempty_paths(tmp_path, settings):
    with pytest.raises(ConfigError, match="tls_cert_path"):
        load_config(_config_path(tmp_path, status_server=settings))


def test_tls_paths_validate_without_reading_certificates(tmp_path):
    plain = load_config(_config_path(tmp_path))
    assert plain.status_server.tls_cert_path is None
    assert plain.status_server.tls_key_path is None
    tls = load_config(_config_path(tmp_path, status_server={
        "tls_cert_path": "/missing/cert.pem", "tls_key_path": "/missing/key.pem",
    }))
    assert tls.status_server.tls_cert_path == "/missing/cert.pem"
    assert not tls.state_db.exists()
