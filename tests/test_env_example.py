from pathlib import Path

from wewln_phonecaller.config import ENV_PREFIX, Settings, find_unknown_variables, load_settings

EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def test_env_example_is_valid_and_safe():
    settings = load_settings(env_file=EXAMPLE)
    assert settings.mode == "simulation"
    assert settings.api_token is None
    assert settings.notion_token is None
    assert find_unknown_variables(environ={}, env_file=EXAMPLE) == []


def test_env_example_documents_every_setting():
    text = EXAMPLE.read_text(encoding="utf-8")
    missing = [n for n in Settings.model_fields if f"{ENV_PREFIX}{n.upper()}=" not in text]
    assert missing == []
