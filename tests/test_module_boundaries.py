"""Prevent the shared model, routing, and SQL layers from acquiring entrypoint dependencies."""
import ast
from pathlib import Path


def test_shared_layers_do_not_import_runtime_or_entrypoints():
    root = Path(__file__).parents[1] / 'src/repowatch'
    paths = [root / 'models.py', root / 'routing.py', *sorted((root / 'storage').glob('*.py'))]
    forbidden = ('repowatch.web', 'repowatch.cli', 'repowatch.runtime', 'repowatch.operations')
    violations = []
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                names = [node.module or '']
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            violations.extend((str(path.relative_to(root)), name) for name in names
                              if any(name == prefix or name.startswith(prefix + '.') for prefix in forbidden))
    assert not violations
