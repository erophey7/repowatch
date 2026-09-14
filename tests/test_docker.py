"""Generator contracts; real image checks are embedded in the Dockerfile."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('docker_generator', ROOT / 'scripts/docker.py')
docker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docker)


@pytest.fixture
def project(tmp_path):
    shutil.copytree(ROOT / 'docker', tmp_path / 'docker')
    shutil.copytree(ROOT / 'src', tmp_path / 'src', ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('LICENSE', 'pyproject.toml', 'Dockerfile', 'Makefile'):
        shutil.copyfile(ROOT / name, tmp_path / name)
    (tmp_path / 'scripts').mkdir()
    shutil.copyfile(ROOT / 'scripts/docker.py', tmp_path / 'scripts/docker.py')
    return tmp_path


def test_default_dockerfile_matches_template():
    features = docker.registry(ROOT)
    assert (ROOT / 'Dockerfile').read_text() == docker.render(ROOT, [], features)


def test_nix_variant_does_not_change_committed_dockerfile(project):
    base = (project / 'Dockerfile').read_bytes()
    features = docker.registry(project)
    filename = docker.generate(project, ['nix'], features)
    assert filename == 'build/docker/nix/Dockerfile'
    generated = (project / filename).read_text()
    assert 'nix-bin' in generated and 'ENV NIX_REMOTE=local' in generated
    assert 'nix-store --add' in generated
    assert generated.index('USER repowatch') < generated.index('nix-store --init')
    assert '"/nix"' in generated and 'nix-daemon --daemon' not in generated
    assert (project / 'Dockerfile').read_bytes() == base
    assert 'nix-bin' not in base.decode()
    docker.generate(project, ['nix'], features)
    assert (project / filename).read_text() == generated
    docker.clean(project)
    assert not (project / '.dockerignore').exists()


@pytest.mark.parametrize('raw', ['nix,nix', 'nix,', 'unknown', '../nix', 'nix\nRUN false', 'c-extension'])
def test_invalid_features_do_not_write_files(project, raw):
    result = subprocess.run([sys.executable, 'scripts/docker.py', 'generate', '--experimental', raw],
                            cwd=project, capture_output=True, text=True)
    assert result.returncode != 0
    assert not (project / '.dockerignore').exists()
    assert not (project / 'build').exists()
    if raw == 'c-extension':
        assert 'not implemented' in result.stderr


def test_explicit_context_excludes_secrets_and_unrelated_files(project):
    for name in ('src/repowatch/local.sqlite3', 'src/repowatch/.env', 'src/repowatch/private.key',
                 'config.yaml', 'secrets.py'):
        (project / name).write_text('SECRET')
    files = docker.context_files(project)
    assert 'src/repowatch/cli.py' in files
    assert 'src/repowatch/static/dashboard.html' in files
    assert not any('SECRET' in (project / name).read_text() for name in files)
    ignore = docker.dockerignore(files)
    assert '\n**\n' in ignore
    assert '!src/repowatch/cli.py\n' in ignore
    assert '!src/repowatch/local.sqlite3' not in ignore
    assert '!config.yaml' not in ignore


def test_symlinks_rejected_before_writing(project, tmp_path):
    (project / 'src/repowatch/secret.py').symlink_to('/etc/passwd')
    with pytest.raises(docker.DockerError, match='symlink'):
        docker.generate(project, [], docker.registry(project))
    assert not (project / '.dockerignore').exists()


def test_check_is_read_only_and_does_not_need_ignore(project):
    result = subprocess.run([sys.executable, 'scripts/docker.py', 'check'], cwd=project)
    assert result.returncode == 0
    assert not (project / '.dockerignore').exists()
    assert not (project / 'build').exists()
    (project / 'Dockerfile').write_text('stale')
    result = subprocess.run([sys.executable, 'scripts/docker.py', 'check'], cwd=project)
    assert result.returncode != 0
    assert (project / 'Dockerfile').read_text() == 'stale'


def test_clean_preserves_foreign_ignore(project):
    (project / '.dockerignore').write_text('handwritten')
    with pytest.raises(docker.DockerError, match='refusing'):
        docker.clean(project)
    assert (project / '.dockerignore').read_text() == 'handwritten'


def test_build_cleans_ignore_on_engine_failure(project, monkeypatch):
    monkeypatch.setattr(docker, 'ROOT', project)
    monkeypatch.setattr(docker.shutil, 'which', lambda _: '/fake/docker')
    commands = []
    def build(args, **kwargs):
        commands.append(args)
        assert (project / '.dockerignore').exists()
        assert (project / args[args.index('--file') + 1]).exists()
        raise subprocess.CalledProcessError(7, args)
    monkeypatch.setattr(docker.subprocess, 'run', build)
    assert docker.main(['build', '--experimental', 'nix']) == 1
    assert commands[0][commands[0].index('--tag') + 1] == 'repowatch:experimental-nix'
    assert not (project / '.dockerignore').exists()
    assert (project / 'Dockerfile').read_text() == docker.render(project, [], docker.registry(project))


def test_conflicting_dockerfile_ignore_rejected(project):
    (project / 'Dockerfile.dockerignore').write_text('!**')
    with pytest.raises(docker.DockerError, match='conflicting'):
        docker.generate(project, [], docker.registry(project))
    assert not (project / '.dockerignore').exists()


def test_dependency_order_conflicts_and_future_build_hook(project):
    feature = project / 'docker/experimental/native'
    feature.mkdir()
    manifest = {'description': 'Test build stage only', 'build_packages': ['gcc'],
                'runtime_packages': [], 'requires': ['nix'], 'conflicts': [], 'volumes': []}
    (feature / 'feature.json').write_text(json.dumps(manifest))
    (feature / 'build.Dockerfile').write_text('ENV ENABLE_NATIVE=1\n')
    features = docker.registry(project)
    assert docker.selection('native,nix', features) == ['nix', 'native']
    with pytest.raises(docker.DockerError, match='required'):
        docker.selection('native', features)
    generated = docker.render(project, docker.selection('native,nix', features), features)
    builder, runtime = generated.split('AS runtime', 1)
    assert 'gcc' in builder and 'ENABLE_NATIVE' in builder
    assert 'gcc' not in runtime and 'ENABLE_NATIVE' not in runtime
    features['native']['conflicts'] = ['nix']
    with pytest.raises(docker.DockerError, match='conflicting'):
        docker.selection('native,nix', features)


def test_make_generate_check_clean(project):
    for target in ('docker-generate', 'docker-check', 'docker-clean'):
        subprocess.run(['make', target, 'DOCKER_EXPERIMENTAL=nix', f'PYTHON={sys.executable}'],
                       cwd=project, check=True, capture_output=True)
    assert not (project / '.dockerignore').exists()
    assert (project / 'build/docker/nix/Dockerfile').exists()


def test_missing_engine_does_not_generate_files(project, monkeypatch):
    monkeypatch.setattr(docker, 'ROOT', project)
    monkeypatch.setattr(docker.shutil, 'which', lambda _: None)
    assert docker.main(['build']) == 1
    assert not (project / '.dockerignore').exists()
    assert not (project / 'build').exists()


def test_build_success_cleans_context_and_passes_tag_without_a_shell(project, monkeypatch):
    monkeypatch.setattr(docker, 'ROOT', project)
    monkeypatch.setattr(docker.shutil, 'which', lambda _: '/fake/docker')
    monkeypatch.setenv('DOCKER_IMAGE', 'repowatch:local;echo-not-a-command')
    commands = []
    monkeypatch.setattr(docker.subprocess, 'run', lambda args, **kwargs: commands.append((args, kwargs)))
    assert docker.main(['build']) == 0
    args, kwargs = commands[0]
    assert args == ['docker', 'build', '--file', 'Dockerfile', '--tag',
                    'repowatch:local;echo-not-a-command', '.']
    assert not kwargs.get('shell')
    assert not (project / '.dockerignore').exists()


def test_private_local_nix_store_without_a_daemon(tmp_path):
    """Native CLI check of the local-store model; not an image-build substitute."""
    import os
    if not shutil.which('nix-store') or not shutil.which('nix-instantiate'):
        pytest.skip('optional native Nix CLI unavailable')
    home = tmp_path / 'home'
    home.mkdir()
    env = dict(os.environ, HOME=str(home), NIX_REMOTE='local?root=' + str(tmp_path / 'root'),
               NIX_CONFIG='build-users-group =', NIX_USER_CONF_FILES='/dev/null')
    for args in (['nix-store', '--init'],
                 ['nix-store', '--add', str(ROOT / 'docker/experimental/nix/feature.json')],
                 ['nix-instantiate', '--eval', '--strict', '--expr', '1 + 1']):
        result = subprocess.run([*args, '--store', 'local?root=' + str(tmp_path / 'root')],
                                env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '2'
    assert list((tmp_path / 'root/nix/store').glob('*feature.json'))


@pytest.mark.parametrize('fail_nginx', [False, True])
def test_compose_build_uses_matching_app_image_and_cleans_both_contexts(project, monkeypatch, fail_nginx):
    monkeypatch.setattr(docker, 'ROOT', project)
    monkeypatch.setattr(docker.shutil, 'which', lambda _: '/fake/docker')
    monkeypatch.setenv('DOCKER_IMAGE', 'repowatch:test-nix')
    calls = []
    def build(args, **kwargs):
        calls.append(args)
        assert (kwargs['cwd'] / '.dockerignore').exists()
        if len(calls) == 2:
            assert kwargs['cwd'] == project / 'docker/nginx'
            assert 'REPOWATCH_IMAGE=repowatch:test-nix' in args
            if fail_nginx:
                raise subprocess.CalledProcessError(9, args)
    monkeypatch.setattr(docker.subprocess, 'run', build)
    assert docker.main(['compose-build', '--experimental', 'nix']) == int(fail_nginx)
    assert len(calls) == 2
    assert not (project / '.dockerignore').exists()
    assert not (project / 'docker/nginx/.dockerignore').exists()



def test_release_bootstrap_is_bundled_without_local_configuration(project):
    (project / 'docker/compose/config.yaml').write_text('LOCAL SECRET')
    files = docker.context_files(project)
    assert 'docker/compose/init.py' in files
    assert 'docker/compose/config.example.yaml' in files
    assert 'docker/compose/config.yaml' not in files
    generated = docker.render(project, [], docker.registry(project))
    assert 'COPY docker/compose/init.py /bootstrap/init.py' in generated
    assert 'COPY docker/compose/config.example.yaml /bootstrap/config.yaml' in generated
    assert generated.index('AS runtime') < generated.index('COPY docker/compose/init.py')
