import gzip

from repowatch.cli.main import main


def _write_config(tmp_path, repos_yaml: str = ""):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
state_db: {tmp_path / "state.sqlite3"}
cache_base_url: http://127.0.0.1:8080
repos:
  - id: alpine-test
    type: apk
    upstream: https://example.org/alpine/v3.20/main
    arch: x86_64
{repos_yaml}
"""
    )
    return config_path


def test_check_config_valid_returns_zero(tmp_path, capsys):
    config_path = _write_config(tmp_path)

    rc = main(["-c", str(config_path), "check-config"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "is valid" in out
    assert "alpine-test" in out


def test_check_config_valid_does_not_create_state_db(tmp_path):
    config_path = _write_config(tmp_path)

    main(["-c", str(config_path), "check-config"])

    assert not (tmp_path / "state.sqlite3").exists()


def test_stats_prints_db_size_and_table_counts(tmp_path, capsys):
    config_path = _write_config(tmp_path)

    rc = main(["-c", str(config_path), "stats"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "state_db:" in out
    assert "bytes)" in out
    for table in ("repo_packages", "repo_events", "request_events", "warmed_packages", "prefetch_bans"):
        assert f"{table}: 0 row(s)" in out
    assert "cache_dir" not in out


def test_stats_cache_dir_flag_reports_not_set_without_nginx_cache_dir(tmp_path, capsys):
    config_path = _write_config(tmp_path)

    rc = main(["-c", str(config_path), "stats", "--cache-dir"])

    assert rc == 0
    assert "not set in config.yaml" in capsys.readouterr().out


def test_stats_cache_dir_flag_walks_the_configured_directory(tmp_path, capsys):
    cache_dir = tmp_path / "nginx-cache"
    (cache_dir / "1").mkdir(parents=True)
    (cache_dir / "1" / "f").write_bytes(b"x" * 42)
    config_path = _write_config(tmp_path, f"nginx:\n  enabled: true\n  cache_dir: {cache_dir}\n")

    rc = main(["-c", str(config_path), "stats", "--cache-dir"])

    assert rc == 0
    out = capsys.readouterr().out
    assert str(cache_dir) in out
    assert "42 bytes" in out
    assert "1 file(s)" in out


def test_stats_cache_dir_flag_fails_clearly_for_a_missing_directory(tmp_path, capsys):
    missing = tmp_path / "does-not-exist"
    config_path = _write_config(tmp_path, f"nginx:\n  enabled: true\n  cache_dir: {missing}\n")

    rc = main(["-c", str(config_path), "stats", "--cache-dir"])

    assert rc == 1
    assert "error" in capsys.readouterr().err


def test_stats_cache_dir_flag_prefers_cache_probe_when_enabled(tmp_path, capsys):
    """docs_dev/ROADMAP.md item 8 hooked up to item 27 — with
    nginx.enable_cache_probe on, `stats --cache-dir` uses the njs-based
    ground-truth scan instead of os.walk(), even without nginx.cache_dir
    set locally."""
    import httpx
    from unittest.mock import patch

    config_path = _write_config(tmp_path, "nginx:\n  enabled: true\n  enable_cache_probe: true\n")

    def handler(request):
        if request.url.params.get("dir") == "c/29":
            return httpx.Response(200, json=[{"file": "f1", "key": "k1", "size": 42}])
        return httpx.Response(200, json=[])

    real_async_client = httpx.AsyncClient
    with patch("repowatch.cache.probe.httpx.AsyncClient",
               lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
        rc = main(["-c", str(config_path), "stats", "--cache-dir"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "via cache-probe" in out
    assert "42 bytes" in out
    assert "1 file(s)" in out


def test_check_config_invalid_returns_one(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("this is not valid yaml: [unclosed")

    rc = main(["-c", str(config_path), "check-config"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "configuration error" in err


def test_check_config_missing_file_returns_one(tmp_path, capsys):
    rc = main(["-c", str(tmp_path / "does-not-exist.yaml"), "check-config"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "configuration error" in err


def test_check_config_tls_pair_validation_does_not_create_db(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    original = config_path.read_text()
    config_path.write_text(original + '\nstatus_server:\n  tls_cert_path: /missing/cert.pem\n')
    assert main(["-c", str(config_path), "check-config"]) == 1
    assert "tls_key_path" in capsys.readouterr().err
    config_path.write_text(original + '\nstatus_server:\n  tls_cert_path: /missing/cert.pem\n  tls_key_path: /missing/key.pem\n')
    assert main(["-c", str(config_path), "check-config"]) == 0
    assert not (tmp_path / "state.sqlite3").exists()


def test_backup_command_writes_gzipped_backup(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    backup_dir = tmp_path / "backups"

    rc = main(["-c", str(config_path), "backup", str(backup_dir)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "backup:" in out
    backups = list(backup_dir.glob("state-*.sqlite3.gz"))
    assert len(backups) == 1
    with gzip.open(backups[0], "rb") as f:
        assert f.read().startswith(b"SQLite format 3")


def test_backup_command_respects_retention_days(tmp_path):
    import os
    import time

    config_path = _write_config(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    stale = backup_dir / "state-stale.sqlite3.gz"
    stale.write_bytes(b"old")
    old_time = time.time() - 30 * 86400
    os.utime(stale, (old_time, old_time))

    rc = main(["-c", str(config_path), "backup", str(backup_dir), "--retention-days", "14"])

    assert rc == 0
    assert not stale.exists()


def test_self_update_does_not_require_a_config_file(monkeypatch, tmp_path, capsys):
    # No -c given, and no config.yaml exists at the default path either —
    # self-update must not touch load_config at all.
    monkeypatch.setattr("repowatch._paths.DEFAULT_CONFIG_PATH", str(tmp_path / "does-not-exist.yaml"))
    monkeypatch.setattr("repowatch.cli.parser.DEFAULT_CONFIG_PATH", str(tmp_path / "does-not-exist.yaml"))

    async def fake_run_self_update(repo, *, check_only=False, force=False):
        assert repo == "owner/name"
        assert check_only is True
        assert force is False
        return "already up to date: installed 1.0.0, latest release is v1.0.0"

    monkeypatch.setattr("repowatch.selfupdate.run_self_update", fake_run_self_update)

    rc = main(["self-update", "--repo", "owner/name", "--check"])

    assert rc == 0
    assert "already up to date" in capsys.readouterr().out


def test_self_update_reports_error_and_returns_one(monkeypatch, capsys):
    from repowatch.selfupdate import SelfUpdateError

    async def failing(repo, *, check_only=False, force=False):
        raise SelfUpdateError("no releases found for owner/name")

    monkeypatch.setattr("repowatch.selfupdate.run_self_update", failing)

    rc = main(["self-update", "--repo", "owner/name"])

    assert rc == 1
    assert "no releases found" in capsys.readouterr().err
