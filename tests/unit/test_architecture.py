import ast
from pathlib import Path


def test_domain_application_and_api_respect_dependency_boundaries():
    root = Path(__file__).parents[2] / "app"
    for layer in ("domain", "application", "api", "agents"):
        for source in (root / layer).rglob("*.py"):
            imports = []
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
            for module in imports:
                assert not module.startswith(("app.infrastructure", "openai", "sqlalchemy")), source
                if layer == "domain":
                    assert not module.startswith(("app.application", "app.api", "fastapi")), source
                if layer == "application":
                    assert not module.startswith(("app.api", "fastapi")), source
