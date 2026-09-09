from dataclasses import replace
from pathlib import Path

import pytest

from repowatch.config import Config, ConfigError, RepoConfig, StatusServerConfig
from repowatch.nginx import render
from repowatch.prefetch import _apt_top_segment, _build_warm_url, _repo_url_prefix
from repowatch.syslog_listener import match_repo_id
from repowatch.url_templates import expand


def repo(**kwargs):
    return RepoConfig(**dict({'id':'core', 'type':'pacman', 'repo_name':'core',
        'upstream':'https://example.test/core/os/x86_64', 'arch':'x86_64'}, **kwargs))


def config(*repos):
    return Config(Path('/tmp/unused'), 300, 'http://cache:8080', StatusServerConfig(), repos=list(repos))


def test_custom_template_shared_by_nginx_warm_and_syslog():
    r = repo(url_template='/{distro}/{arch}/{repo_name}/', url_variables={'distro':'archlinux'})
    c = config(r)
    assert expand(r) == '/archlinux/x86_64/core'
    assert _build_warm_url(c, r, 'package.pkg.tar.zst') == 'http://cache:8080/archlinux/x86_64/core/package.pkg.tar.zst'
    assert match_repo_id('/archlinux/x86_64/core/package.pkg.tar.zst', c.repos) == 'core'
    assert match_repo_id('/arch/core/os/x86_64/package.pkg.tar.zst', c.repos) is None
    text = render(c)
    assert 'location /archlinux/x86_64/core/' in text
    assert 'rewrite ^/archlinux/x86_64/core/(.*)$ /core/os/x86_64/$1 break;' in text
    assert r.upstream == 'https://example.test/core/os/x86_64'


def test_apt_shared_root_keeps_component_pool_mapping():
    a = repo(type='apt', distribution='noble', component='main', upstream='https://example.test/ubuntu', url_template='/custom/{distribution}/')
    b = replace(a, id='universe', component='universe')
    c = config(a,b)
    assert _apt_top_segment(a) == '/custom/noble'
    assert _build_warm_url(c,a,'pool/main/a/a.deb') == 'http://cache:8080/custom/noble/pool/main/a/a.deb'
    assert match_repo_id('/custom/noble/pool/universe/p/package.deb', c.repos) == 'universe'
    assert render(c).count('location /custom/noble/') == 1


def test_apk_template_does_not_append_arch_twice():
    r = repo(type='apk', upstream='https://example.test/alpine/v3/main', url_template='/custom/{arch}/')
    assert _repo_url_prefix(r) == '/custom/x86_64'
    assert 'rewrite ^/custom/x86_64/(.*)$ /alpine/v3/main/x86_64/$1 break;' in render(config(r))


def test_template_conflicts_rejected():
    a = repo(url_template='/same/')
    with pytest.raises(ConfigError, match='conflicting'):
        render(config(a, replace(a, id='extra', upstream='https://other.test/extra')))
    with pytest.raises(ConfigError, match='overlapping'):
        render(config(a, replace(a, id='extra', url_template='/same/extra/')))


@pytest.mark.parametrize('template', ['/', '//host/a', '/a/../b', '/{unknown}', '/{arch.__class__}', '/{arch[0]}',
    '/{arch!r}', '/{arch:>4}', '/{distribution}', '/a;include', '/a/$arg_url', '/a?x=1', '/a#b', '/a/{', '/a%2f..'])
def test_unsafe_templates_rejected(template):
    with pytest.raises(ConfigError):
        repo(url_template=template)


@pytest.mark.parametrize('variables', [[], {'arch':'other'}, {'branch':'../p11'}, {'branch':1}, {'branch':'p11/branch'}])
def test_custom_variables_are_safe_segments(variables):
    with pytest.raises(ConfigError):
        repo(url_template='/{branch}/{arch}', url_variables=variables)


def test_defaults_unchanged_and_unused_variables_rejected():
    r=repo()
    assert expand(r) is None
    assert _repo_url_prefix(r) == '/arch/core/os/x86_64'
    with pytest.raises(ConfigError):
        repo(url_variables={'branch':'p11'})
