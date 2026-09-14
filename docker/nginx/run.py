"""Trial nginx supervisor: use the existing validated apply/rollback transaction."""
from pathlib import Path
import logging
import os
import signal
import subprocess
import tempfile
import threading

from repowatch.config import ConfigError, load_config
from repowatch.nginx import apply

CONFIG = Path('/etc/repowatch/config.yaml')
DIRECTORY = Path('/etc/nginx/repowatch')
POLICY = DIRECTORY / 'policy.json'
READY = Path('/run/repowatch-nginx.ready')
logger = logging.getLogger(__name__)


def snapshot(source: Path, destination: Path) -> None:
    """Validate a private copy so a later YAML edit cannot change this apply."""
    data = source.read_text()
    with tempfile.NamedTemporaryFile(mode='w', dir=destination.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(data)
            output.flush()
            config = load_config(temporary)
            if (str(config.state_db) != '/var/lib/repowatch/state.sqlite3'
                    or config.cache_base_url != 'http://127.0.0.1:8080'
                    or not config.nginx.enabled
                    or config.nginx.listen not in ('8080', '0.0.0.0:8080')
                    or config.nginx.cache_dir != '/var/cache/nginx/repowatch'):
                raise ConfigError('trial requires its fixed state, cache paths and nginx port')
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def shutdown(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGQUIT)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    os.umask(0o002)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    READY.unlink(missing_ok=True)
    # Start with only the private health server. Readiness remains false until
    # the first complete configuration has passed nginx -t and been applied.
    (DIRECTORY / 'active.conf').write_text('# Initial trial configuration\n')
    process = subprocess.Popen(['/usr/sbin/nginx', '-c', '/etc/nginx/nginx.conf', '-g', 'daemon off;'])
    try:
        while not stop.is_set():
            if process.poll() is not None:
                logger.error('nginx exited unexpectedly')
                return 1
            try:
                private = DIRECTORY / 'input.yaml'
                snapshot(CONFIG, private)
                apply(str(private), str(POLICY), use_systemctl=False)
                READY.touch()
            except (ConfigError, OSError, ValueError, subprocess.SubprocessError):
                logger.exception('trial nginx configuration not applied; keeping last active configuration')
            stop.wait(5)
    finally:
        READY.unlink(missing_ok=True)
        shutdown(process)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
