from pathlib import Path

import pytest

from repowatch.config import ConfigError, RepoConfig, load_config

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config" / "config.example.yaml"


def test_example_config_loads():
    config = load_config(EXAMPLE_CONFIG)
    assert len(config.repos) == 3
    assert config.repo_by_id("arch-core-x86_64").type == "pacman"
    assert config.repo_by_id("debian-bookworm-main").type == "apt"
    assert config.repo_by_id("rocky-9-baseos-x86_64").type == "dnf"


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
    from repowatch.config import Config, StatusServerConfig

    config = Config(
        state_db="/tmp/x.sqlite3", check_interval=300,
        cache_base_url="http://127.0.0.1:8080", status_server=StatusServerConfig(),
    )
    repo = RepoConfig(id="r", type="apk", upstream="https://example.org", arch="x86_64")
    assert config.effective_check_interval(repo) == 300


def test_effective_check_interval_per_repo_override():
    from repowatch.config import Config, StatusServerConfig

    config = Config(
        state_db="/tmp/x.sqlite3", check_interval=300,
        cache_base_url="http://127.0.0.1:8080", status_server=StatusServerConfig(),
    )
    repo = RepoConfig(
        id="r", type="apk", upstream="https://example.org", arch="x86_64", check_interval=30,
    )
    assert config.effective_check_interval(repo) == 30


def test_effective_prefetch_bandwidth_limit_falls_back_to_global():
    from repowatch.config import Config, StatusServerConfig

    config = Config(
        state_db="/tmp/x.sqlite3", check_interval=300,
        cache_base_url="http://127.0.0.1:8080", status_server=StatusServerConfig(),
        prefetch_bandwidth_limit=5.0,
    )
    repo = RepoConfig(id="r", type="apk", upstream="https://example.org", arch="x86_64")
    assert config.effective_prefetch_bandwidth_limit(repo) == 5.0


def test_effective_prefetch_bandwidth_limit_per_repo_override():
    from repowatch.config import Config, StatusServerConfig

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
    from repowatch.prefetch import _repo_url_prefix
    from repowatch.state import RepoSnapshot, StateStore
    from repowatch.syslog_listener import match_repo_id
    import yaml
    path = tmp_path / 'config.yaml'
    raw = {'state_db': str(tmp_path / 'state'), 'cache_base_url': 'http://localhost', 'repos': [
        {'id': f'core-{arch}', 'type': 'pacman', 'repo_name': 'core', 'arch': arch,
         'upstream': f'https://example.org/core/os/{arch}', 'group': 'arch'}
        for arch in ('x86_64', 'i686')
    ]}
    path.write_text(yaml.safe_dump(raw))
    config = load_config(path)
    store = StateStore(config.state_db)
    for repo in config.repos:
        assert PacmanParser(repo).index_url() == f'https://example.org/core/os/{repo.arch}/core.db.tar.gz'
        filename = f'demo-1-{repo.arch}.pkg.tar.zst'
        store.record_snapshot(RepoSnapshot(repo.id, {'demo-1': filename}, {'demo-1': 'demo'}))
        assert match_repo_id(_repo_url_prefix(repo) + '/' + filename, config.repos) == repo.id
    assert store.get_packages('core-x86_64') == {'demo-1': 'demo-1-x86_64.pkg.tar.zst'}
    assert store.get_packages('core-i686') == {'demo-1': 'demo-1-i686.pkg.tar.zst'}
    store.ban_package('core-x86_64', 'demo')
    assert store.get_banned_packages('core-i686') == []


@pytest.mark.parametrize('backend', ['openssl', 'apk-tools'])
def test_apk_signature_config(backend):
    repo = RepoConfig(id='apk', type='apk', upstream='https://example.org', arch='x86_64',
                      verify_signature=True, apk_keys_dir='/etc/apk/keys', apk_signature_backend=backend)
    assert repo.apk_signature_backend == backend


def test_invalid_apk_backend():
    with pytest.raises(ConfigError):
        RepoConfig(id='apk', type='apk', upstream='https://example.org', arch='x86_64', apk_signature_backend='auto')
