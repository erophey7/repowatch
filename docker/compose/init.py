"""Initialize trial bind mounts without replacing existing configuration/data."""
from pathlib import Path
import os
import shutil

from repowatch.config.load import load_config
from repowatch.runtime.context import ServiceState


def initialize(config_dir: Path, state_dir: Path, nix_dir: Path, seed: Path,
               owner: tuple[int, int] | None = (10001, 10001),
               cache_dir: Path | None = None) -> None:
    if owner is not None and any(type(value) is not int or not 0 < value < 2**31 for value in owner):
        raise ValueError('trial UID/GID must be positive integers below 2147483648')
    config_path = config_dir / 'config.yaml'
    state_path = state_dir / 'state.sqlite3'
    keys_dir = config_dir / 'keys'
    paths = [config_dir, state_dir, nix_dir, keys_dir, config_path, state_path]
    if cache_dir is not None:
        paths.append(cache_dir)
    for path in paths:
        if path.is_symlink():
            raise ValueError(f'symlink not supported in trial mounts: {path}')
    for directory in (config_dir, state_dir, nix_dir, keys_dir):
        directory.mkdir(parents=True, exist_ok=True)
        if owner is not None:
            os.chown(directory, *owner)
        directory.chmod(0o755 if directory == nix_dir else 0o2770)
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if owner is not None:
            # Debian nginx workers use www-data (UID 33). Bind mounts do not
            # inherit the ownership prepared in the image, unlike fresh volumes.
            os.chown(cache_dir, 33, owner[1])
        cache_dir.chmod(0o2770)
    if not config_path.exists():
        # Validate the seed before creating the persistent configuration.
        load_config(seed)
        shutil.copyfile(seed, config_path)
    config = load_config(config_path)
    if str(config.state_db) != '/var/lib/repowatch/state.sqlite3':
        raise ValueError('trial state_db must be /var/lib/repowatch/state.sqlite3')
    config_path.chmod(0o660)
    if owner is not None:
        os.chown(config_path, *owner)
        os.setgroups([])
        os.setgid(owner[1])
        os.setuid(owner[0])
    # Create/migrate SQLite as the application user. Setgid directories and
    # group-writable SQLite files allow the nginx helper's dedup reads too.
    os.umask(0o007)
    store = ServiceState(state_path)
    state_path.chmod(0o660)
    del store


if __name__ == '__main__':
    initialize(Path('/etc/repowatch'), Path('/var/lib/repowatch'), Path('/nix'),
               Path('/bootstrap/config.yaml'),
               owner=(10001, int(os.environ.get('REPOWATCH_GID', '10001'))),
               cache_dir=Path('/var/cache/nginx/repowatch'))
