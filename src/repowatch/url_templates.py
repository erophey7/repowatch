"""Local repository path templates, independent of upstream URLs and parsers."""

from __future__ import annotations

import re
from repowatch.errors import ConfigError
from string import Formatter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from repowatch.config.models import RepoConfig

FIELDS = ('id', 'type', 'repo_name', 'arch', 'distribution', 'component')


def expand(repo: RepoConfig) -> str | None:
    variables = repo.url_variables
    if not isinstance(variables, dict):
        raise ConfigError('url_variables: must be a dict of strings')
    for name, value in variables.items():
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', name) or name in FIELDS:
            raise ConfigError('url_variables: invalid name, or it overrides a standard field')
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_+~.-]+', value) or value in ('.', '..'):
            raise ConfigError('url_variables: values must be a single safe URL segment')
    template = repo.url_template
    if template is None:
        if variables:
            raise ConfigError('url_variables requires url_template')
        return None
    if not isinstance(template, str) or not template or len(template) > 1024:
        raise ConfigError('url_template: must be a nonempty string up to 1024 characters')
    values = {name: getattr(repo, name) for name in FIELDS} | variables
    parts = []
    try:
        for literal, name, spec, conversion in Formatter().parse(template):
            parts.append(literal)
            if name is None:
                continue
            if name not in values or spec or conversion:
                raise ConfigError('url_template: unknown variable; format/conversion are not allowed')
            value = values[name]
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_+~.-]+', value) or value in ('.', '..'):
                raise ConfigError(f'url_template: field {name} is missing or is not a valid URL segment')
            parts.append(value)
    except ValueError as exc:
        raise ConfigError('url_template: invalid curly braces') from exc
    path = ''.join(parts).rstrip('/')
    if (not re.fullmatch(r'/[A-Za-z0-9_./+~-]+', path) or '//' in path
            or any(part in ('.', '..') for part in path.split('/'))):
        raise ConfigError('url_template: must be an absolute local path with no traversal/nginx syntax')
    return path
