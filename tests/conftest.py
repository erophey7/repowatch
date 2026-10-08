"""Shared isolation for integration tests using system nginx."""

import pytest


@pytest.fixture
def nginx_http_paths(tmp_path):
    """Keep nginx temporary files writable by the test user on every distro."""
    directives = []
    for kind in ('client_body', 'proxy', 'fastcgi', 'uwsgi', 'scgi'):
        directory = tmp_path / (kind + '_temp')
        directory.mkdir()
        directives.append(f'{kind}_temp_path "{directory}";')
    return '\n'.join(directives) + '\n'
