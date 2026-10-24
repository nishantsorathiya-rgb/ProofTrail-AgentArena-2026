import getpass
import json
import subprocess

import pytest
from dotenv import dotenv_values

from scripts import configure_tavily

KEY = "tvly-" + "offline_private_fixture_" * 2


@pytest.fixture
def private_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    return tmp_path


def test_noninteractive_entry_refuses_before_prompt_or_file_write(private_repo, monkeypatch):
    monkeypatch.setattr(configure_tavily.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(getpass, "getpass", lambda _: pytest.fail("visible input"))
    with pytest.raises(ValueError):
        configure_tavily.enter_key(private_repo)
    assert not (private_repo / ".env").exists()


def test_masked_entry_preserves_mesh_and_disables_live(private_repo, monkeypatch, capsys):
    target = private_repo / ".env"
    target.write_text(
        "MESH_API_KEY='offline-preserved-mesh'\nENABLE_LIVE_REQUESTS='true'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(configure_tavily.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(getpass, "getpass", lambda _: KEY)
    configure_tavily.enter_key(private_repo)
    values = dotenv_values(target)
    assert values["TAVILY_API_KEY"] == KEY
    assert values["MESH_API_KEY"] == "offline-preserved-mesh"
    assert values["ENABLE_LIVE_REQUESTS"] == "false"
    assert KEY not in str(capsys.readouterr())


def test_invalid_key_preserves_existing_file(private_repo, monkeypatch):
    target = private_repo / ".env"
    original = "TAVILY_API_KEY='offline-existing'\nENABLE_LIVE_REQUESTS='false'\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(configure_tavily.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(getpass, "getpass", lambda _: "invalid")
    with pytest.raises(ValueError):
        configure_tavily.enter_key(private_repo)
    assert target.read_text(encoding="utf-8") == original


def test_getpass_warning_refuses_visible_fallback(private_repo, monkeypatch):
    import warnings

    def warning(_):
        warnings.warn("No masked terminal", getpass.GetPassWarning, stacklevel=2)
        return KEY

    monkeypatch.setattr(configure_tavily.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(getpass, "getpass", warning)
    with pytest.raises(getpass.GetPassWarning):
        configure_tavily.enter_key(private_repo)
    assert not (private_repo / ".env").exists()


def test_tracked_env_refuses_before_secret_prompt(private_repo, monkeypatch):
    target = private_repo / ".env"
    target.write_text("placeholder", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-f", ".env"], cwd=private_repo, check=True, capture_output=True
    )
    monkeypatch.setattr(getpass, "getpass", lambda _: pytest.fail("premature prompt"))
    with pytest.raises(ValueError):
        configure_tavily.enter_key(private_repo)
    assert target.read_text(encoding="utf-8") == "placeholder"


def test_local_check_never_authenticates_or_reveals_key(private_repo, monkeypatch):
    import httpx

    (private_repo / ".env").write_text(f"TAVILY_API_KEY='{KEY}'\n", encoding="utf-8")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: pytest.fail("network client"))
    result = configure_tavily.check(private_repo)
    assert result["key_configured_with_plausible_format"]
    assert result["api_requests"] == 0 and not result["credentials_authenticated"]
    assert not result["account_credits_checked"]
    assert KEY not in json.dumps(result)
