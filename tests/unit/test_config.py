import pytest
from pydantic import ValidationError

from app.application.config import Settings


def test_defaults_are_offline_and_secrets_are_not_serialized():
    config = Settings(_env_file=None, mesh_api_key="fake-private-key", tavily_api_key="fake-tavily")
    assert config.enable_live_requests is False
    assert config.mesh_base_url == "https://api.meshapi.ai/v1"
    assert "fake-private-key" not in repr(config)
    assert "fake-tavily" not in config.model_dump_json()
    assert config.max_llm_calls_per_run == 12
    assert config.max_retries_per_subquestion == 2


def test_env_and_dotenv_load_without_exposing_keys(monkeypatch, tmp_path):
    dotenv = tmp_path / "config.env"
    dotenv.write_text(
        "MESH_API_KEY=fixture-env-key\nMESH_MODEL_ID=fixture-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MAX_LLM_CALLS_PER_RUN", "3")
    config = Settings(_env_file=dotenv)
    assert config.mesh_api_key.get_secret_value() == "fixture-env-key"
    assert config.mesh_model_id == "fixture-model"
    assert config.max_llm_calls_per_run == 3


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.openai.com/v1",
        "https://api.meshapi.ai.evil.example/v1",
        "http://api.meshapi.ai/v1",
        "https://api.meshapi.ai/v1/",
    ],
)
def test_other_model_routes_are_rejected(base_url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mesh_base_url=base_url)


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_llm_calls_per_run": 0},
        {"max_parallel_fetches": -1},
        {"max_retries_per_subquestion": 3},
        {"http_timeout_seconds": 0},
        {"database_url": "postgresql://user:private@host/db"},
        {"database_url": "sqlite:///db?mode=rw"},
    ],
)
def test_invalid_configuration_fails_closed_without_echoing_inputs(overrides):
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **overrides)
    assert "user:private" not in str(error.value)
