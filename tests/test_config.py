from pathlib import Path

import pytest

from repowatch.errors import ConfigError
from repowatch.config.models import RepoConfig
from repowatch.config.load import load_config

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config" / "config.example.yaml"


def test_example_config_loads():
    config = load_config(EXAMPLE_CONFIG)
    assert config.repos == []
    assert config.nginx.enable_purge and config.nginx.enable_dedup and config.nginx.enable_cache_probe
    assert config.syslog_listener.enabled and config.status_server.token_repo_restrictions
    assert config.prefetch_bandwidth_limit == 10 * 1024 * 1024


def test_missing_config_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.yaml")


def test_malformed_yaml_raises_config_error_not_yaml_error(tmp_path):
    bad_config = tmp_path / "broken.yaml"
    bad_config.write_text("this is not valid yaml: [unclosed")
    with pytest.raises(ConfigError):
        load_config(bad_config)


def test_non_mapping_top_level_raises_config_error(tmp_path):
    bad_config = tmp_path / "list.yaml"
    bad_config.write_text("- just\n- a\n- list\n")
    with pytest.raises(ConfigError):
        load_config(bad_config)


def test_browse_base_url_falls_back_to_cache_base_url(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
state_db: /tmp/repowatch-test.sqlite3
cache_base_url: http://127.0.0.1:8080
repos:
  - id: r
    type: apk
    upstream: https://example.org
    arch: x86_64
"""
    )
    config = load_config(config_path)
    assert config.public_cache_url is None
    assert config.browse_base_url == "http://127.0.0.1:8080"


def test_public_cache_url_overrides_browse_base_url(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
state_db: /tmp/repowatch-test.sqlite3
cache_base_url: http://127.0.0.1:8080
public_cache_url: "http://192.0.2.10:8080/"
repos:
  - id: r
    type: apk
    upstream: https://example.org
    arch: x86_64
"""
    )
    config = load_config(config_path)
    # trailing slash is stripped, same as with cache_base_url
    assert config.public_cache_url == "http://192.0.2.10:8080"
    assert config.browse_base_url == "http://192.0.2.10:8080"


def test_verify_signature_requires_keyring_path():
    with pytest.raises(ConfigError):
        RepoConfig(
            id="r", type="pacman", upstream="https://example.org", arch="x86_64",
            repo_name="core", verify_signature=True,
        )


def test_verify_signature_with_keyring_path_ok():
    repo = RepoConfig(
        id="r", type="pacman", upstream="https://example.org", arch="x86_64",
        repo_name="core", verify_signature=True, keyring_path="/some/keyring.gpg",
    )
    assert repo.verify_signature is True


def test_apk_signature_requires_apk_keys_not_gpg_keyring():
    """APK trusts a directory of RSA keys, not a GPG keyring."""
    with pytest.raises(ConfigError):
        RepoConfig(
            id="r", type="apk", upstream="https://example.org", arch="x86_64",
            verify_signature=True, keyring_path="/some/keyring.gpg",
        )


def test_repo_config_group_defaults_to_none():
    repo = RepoConfig(id="r", type="apk", upstream="https://example.org", arch="x86_64")
    assert repo.group is None


def test_repo_config_group_is_a_free_form_label():
    repo = RepoConfig(
        id="r", type="apk", upstream="https://example.org", arch="x86_64", group="staging",
    )
    assert repo.group == "staging"


def test_effective_check_interval_falls_back_to_global():
    from repowatch.config.models import Config
    from repowatch.config.models import StatusServerConfig

    config = Config(
        state_db="/tmp/x.sqlite3", check_interval=300,
        cache_base_url="http://127.0.0.1:8080", status_server=StatusServerConfig(),
    )
    repo = RepoConfig(id="r", type="apk", upstream="https://example.org", arch="x86_64")
    assert config.effective_check_interval(repo) == 300


def test_effective_check_interval_per_repo_override():
    from repowatch.config.models import Config
    from repowatch.config.models import StatusServerConfig

    config = Config(
        state_db="/tmp/x.sqlite3", check_interval=300,
        cache_base_url="http://127.0.0.1:8080", status_server=StatusServerConfig(),
    )
    repo = RepoConfig(
        id="r", type="apk", upstream="https://example.org", arch="x86_64", check_interval=30,
    )
    assert config.effective_check_interval(repo) == 30


def test_effective_prefetch_bandwidth_limit_falls_back_to_global():
    from repowatch.config.models import Config
    from repowatch.config.models import StatusServerConfig

    config = Config(
        state_db="/tmp/x.sqlite3", check_interval=300,
        cache_base_url="http://127.0.0.1:8080", status_server=StatusServerConfig(),
        prefetch_bandwidth_limit=5.0,
    )
    repo = RepoConfig(id="r", type="apk", upstream="https://example.org", arch="x86_64")
    assert config.effective_prefetch_bandwidth_limit(repo) == 5.0


def test_effective_prefetch_bandwidth_limit_per_repo_override():
    from repowatch.config.models import Config
    from repowatch.config.models import StatusServerConfig

    config = Config(
        state_db="/tmp/x.sqlite3", check_interval=300,
        cache_base_url="http://127.0.0.1:8080", status_server=StatusServerConfig(),
        prefetch_bandwidth_limit=5.0,
    )
    repo = RepoConfig(
        id="r", type="apk", upstream="https://example.org", arch="x86_64", prefetch_bandwidth_limit=1.0,
    )
    assert config.effective_prefetch_bandwidth_limit(repo) == 1.0


def test_per_repo_check_interval_and_bandwidth_limit_load_from_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
state_db: /tmp/repowatch-test.sqlite3
cache_base_url: http://127.0.0.1:8080
check_interval: 300
prefetch_bandwidth_limit: 10
repos:
  - id: fast-repo
    type: apk
    upstream: https://example.org
    arch: x86_64
    check_interval: 30
    prefetch_bandwidth_limit: 2
  - id: slow-repo
    type: apk
    upstream: https://example.org
    arch: x86_64
"""
    )
    config = load_config(config_path)
    fast = config.repo_by_id("fast-repo")
    slow = config.repo_by_id("slow-repo")

    assert config.effective_check_interval(fast) == 30
    assert config.effective_check_interval(slow) == 300
    assert config.effective_prefetch_bandwidth_limit(fast) == 2.0
    assert config.effective_prefetch_bandwidth_limit(slow) == 10.0


def test_syslog_listener_enabled_defaults_to_true(tmp_path):
    """Changed from opt-in to on-by-default 2026-09-14 — see
    SyslogListenerConfig's own docstring: without real request visibility,
    the hourly warmed_packages expiry (watcher.prune_all) has no way to
    tell "nobody wants this" from "we just can't see it"."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
state_db: /tmp/repowatch-test.sqlite3
cache_base_url: http://127.0.0.1:8080
repos:
  - id: r
    type: apk
    upstream: https://example.org
    arch: x86_64
"""
    )
    config = load_config(config_path)
    assert config.syslog_listener.enabled is True


def test_check_concurrency_defaults_to_eight(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
state_db: /tmp/repowatch-test.sqlite3
cache_base_url: http://127.0.0.1:8080
repos:
  - id: r
    type: apk
    upstream: https://example.org
    arch: x86_64
"""
    )
    config = load_config(config_path)
    assert config.check_concurrency == 8


def test_check_concurrency_loads_from_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
state_db: /tmp/repowatch-test.sqlite3
cache_base_url: http://127.0.0.1:8080
check_concurrency: 3
repos:
  - id: r
    type: apk
    upstream: https://example.org
    arch: x86_64
"""
    )
    config = load_config(config_path)
    assert config.check_concurrency == 3


def test_size_based_retention_fields_default_to_none(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
state_db: /tmp/repowatch-test.sqlite3
cache_base_url: http://127.0.0.1:8080
repos:
  - id: r
    type: apk
    upstream: https://example.org
    arch: x86_64
"""
    )
    config = load_config(config_path)
    assert config.event_max_rows_per_repo is None
    assert config.request_max_rows is None


def test_size_based_retention_fields_load_from_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
state_db: /tmp/repowatch-test.sqlite3
cache_base_url: http://127.0.0.1:8080
event_max_rows_per_repo: 10000
request_max_rows: 1000000
repos:
  - id: r
    type: apk
    upstream: https://example.org
    arch: x86_64
"""
    )
    config = load_config(config_path)
    assert config.event_max_rows_per_repo == 10000
    assert config.request_max_rows == 1000000


def test_duplicate_repo_ids_rejected(tmp_path):
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text(
        """
state_db: /tmp/repowatch-test.sqlite3
cache_base_url: http://127.0.0.1:8080
repos:
  - id: dup
    type: apk
    upstream: https://example.org
    arch: x86_64
  - id: dup
    type: apk
    upstream: https://example.org
    arch: x86_64
"""
    )
    with pytest.raises(ConfigError):
        load_config(bad_config)


def test_multiarch_uses_separate_repo_configs_and_state(tmp_path):
    from repowatch.parsers import PacmanParser
    from repowatch.routing import repo_prefix
    from repowatch.models import RepoSnapshot
    from repowatch.runtime.context import ServiceState
    from repowatch.runtime.syslog import match_repo_id
    import yaml
    path = tmp_path / 'config.yaml'
    raw = {'state_db': str(tmp_path / 'state'), 'cache_base_url': 'http://localhost', 'repos': [
        {'id': f'core-{arch}', 'type': 'pacman', 'repo_name': 'core', 'arch': arch,
         'upstream': f'https://example.org/core/os/{arch}', 'group': 'arch'}
        for arch in ('x86_64', 'i686')
    ]}
    path.write_text(yaml.safe_dump(raw))
    config = load_config(path)
    store = ServiceState(config.state_db)
    for repo in config.repos:
        assert PacmanParser(repo).index_url() == f'https://example.org/core/os/{repo.arch}/core.db.tar.gz'
        filename = f'demo-1-{repo.arch}.pkg.tar.zst'
        store.repositories.record_snapshot(RepoSnapshot(repo.id, {'demo-1': filename}, {'demo-1': 'demo'}))
        assert match_repo_id(repo_prefix(repo) + '/' + filename, config.repos) == repo.id
    assert store.repositories.get_packages('core-x86_64') == {'demo-1': 'demo-1-x86_64.pkg.tar.zst'}
    assert store.repositories.get_packages('core-i686') == {'demo-1': 'demo-1-i686.pkg.tar.zst'}
    store.cache.ban_package('core-x86_64', 'demo')
    assert store.cache.get_banned_packages('core-i686') == []


@pytest.mark.parametrize('backend', ['openssl', 'apk-tools'])
def test_apk_signature_config(backend):
    repo = RepoConfig(id='apk', type='apk', upstream='https://example.org', arch='x86_64',
                      verify_signature=True, apk_keys_dir='/etc/apk/keys', apk_signature_backend=backend)
    assert repo.apk_signature_backend == backend


def test_invalid_apk_backend():
    with pytest.raises(ConfigError):
        RepoConfig(id='apk', type='apk', upstream='https://example.org', arch='x86_64', apk_signature_backend='auto')


@pytest.mark.parametrize('field', [
    'check_interval', 'check_concurrency', 'prefetch_concurrency',
    'notify_after_failures', 'event_retention_days', 'request_retention_days',
    'warmed_retention_days', 'event_max_rows_per_repo', 'request_max_rows',
])
@pytest.mark.parametrize('value', [0, -1, True, 1.5, '12', 'oops', 10**30])
def test_operational_integer_settings_reject_unsafe_values(tmp_path, field, value):
    import yaml
    raw = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    raw[field] = value
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match=field):
        load_config(path)


@pytest.mark.parametrize('field,value', [
    ('prefetch', 'false'), ('verify_signature', 'false'),
    ('prefetch', 0), ('check_interval', 0), ('check_interval', True),
    ('check_interval', '30'), ('check_interval', 1.5),
])
def test_repository_operational_values_are_strict(field, value):
    with pytest.raises(ConfigError, match=field):
        RepoConfig(id='r', type='apk', upstream='https://example.org', arch='x86_64',
                   **{field: value})


def test_config_models_reject_invalid_values_without_yaml():
    from dataclasses import replace
    config = load_config(EXAMPLE_CONFIG)
    with pytest.raises(ConfigError, match='check_concurrency'):
        replace(config, check_concurrency=0)
    with pytest.raises(ConfigError, match='warmed_retention_days'):
        replace(config, warmed_retention_days=-1)
    with pytest.raises(ConfigError, match='key_expiry_warning_days'):
        replace(config, key_expiry_warning_days=-1)
    assert replace(config, key_expiry_warning_days=0).key_expiry_warning_days == 0
    assert replace(config, request_max_rows=None).request_max_rows is None


@pytest.mark.parametrize('failure', [PermissionError('denied'), FileNotFoundError('removed')])
def test_read_failure_is_config_error_with_cause(tmp_path, monkeypatch, failure):
    path = tmp_path / 'config.yaml'
    def fail_read(*args, **kwargs):
        raise failure
    monkeypatch.setattr(Path, 'read_text', fail_read)
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert caught.value.__cause__ is failure


def test_invalid_utf8_is_config_error(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_bytes(b'\xff')
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert isinstance(caught.value.__cause__, UnicodeError)


def test_syslog_enabled_rejects_string_false():
    from repowatch.config.models import SyslogListenerConfig
    with pytest.raises(ConfigError, match='enabled'):
        SyslogListenerConfig(enabled='false')


@pytest.mark.parametrize("value", ["null", "{}", "false", "example", "0"])
def test_empty_repositories_requires_a_list(tmp_path, value):
    path = tmp_path / "config.yaml"
    path.write_text(f"state_db: {tmp_path / 'state.sqlite3'}\n"
                    f"cache_base_url: http://127.0.0.1:8080\nrepos: {value}\n")
    with pytest.raises(ConfigError, match="repos must be a list"):
        load_config(path)


def test_empty_installation_checks_and_reports_without_fetching(tmp_path, monkeypatch):
    import asyncio
    import repowatch.runtime.scheduler as scheduler
    from repowatch.reporting.status import healthz_payload, status_payload
    from repowatch.runtime.context import ServiceState

    path = tmp_path / "config.yaml"
    path.write_text(f"state_db: {tmp_path / 'state.sqlite3'}\n"
                    "cache_base_url: http://127.0.0.1:8080\nrepos: []\n")
    config = load_config(path)
    assert not config.state_db.exists()

    async def unexpected_check(*args, **kwargs):
        pytest.fail("an empty installation must not check an upstream")

    monkeypatch.setattr(scheduler, "check_repo", unexpected_check)
    store = ServiceState(config.state_db)
    assert asyncio.run(scheduler.check_all(config, store)) == []
    assert status_payload(path, store) == (200, {})
    assert healthz_payload(path, store) == (200, {"ok": True})


@pytest.mark.parametrize('backend', ['SafeLoader', 'CSafeLoader'])
def test_safe_yaml_backends_preserve_aliases_and_reject_objects(tmp_path, monkeypatch, backend):
    import importlib
    import yaml
    module = importlib.import_module('repowatch.config.load')
    loader = getattr(yaml, backend, None)
    if loader is None:
        pytest.skip('PyYAML C backend unavailable')
    monkeypatch.setattr(module, '_SAFE_LOADER', loader)
    path = tmp_path / 'config.yaml'
    path.write_text('state_db: /tmp/unused.sqlite3\ncache_base_url: &url http://127.0.0.1:8080\n'
                    'public_cache_url: *url\nrepos: []\n')
    config = load_config(path)
    assert config.cache_base_url == config.public_cache_url
    for invalid in ('state_db: [broken', '!!python/object:builtins.object {}'):
        path.write_text(invalid)
        with pytest.raises(ConfigError):
            load_config(path)
