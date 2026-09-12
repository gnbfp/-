import pytest

from src.config import ConfigError, load_config, parse_env_file


def test_parse_env_file_ok(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# 注释\n\nLLM_MODEL=deepseek-flash\nLLM_API_KEY=\"sk-abc\"\n", encoding="utf-8"
    )
    assert parse_env_file(env) == {
        "LLM_MODEL": "deepseek-flash",
        "LLM_API_KEY": "sk-abc",
    }


def test_parse_env_file_rejects_bare_value(tmp_path):
    env = tmp_path / ".env"
    env.write_text("sk-abcdef0123456789\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        parse_env_file(env)
    assert "LLM_API_KEY=" in str(exc.value)


def test_missing_env_file_is_empty(tmp_path):
    assert parse_env_file(tmp_path / "nope") == {}


def test_repr_never_leaks_secret(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=sk-supersecret\nLLM_MODEL=deepseek-flash\n", encoding="utf-8")
    cfg = load_config(env, data_dir=tmp_path / "data")
    for text in (repr(cfg), str(cfg)):
        assert "supersecret" not in text
        assert "sk-" not in text
    assert "已设置" in repr(cfg)


def test_relative_data_dir_resolves_to_repo_root(tmp_path):
    cfg = load_config(tmp_path / "none", data_dir=None)
    assert cfg.data_dir.is_absolute()


def test_check_reports_missing_names(tmp_path):
    cfg = load_config(tmp_path / "none", data_dir=tmp_path)
    with pytest.raises(ConfigError) as exc:
        cfg.check_llm()
    assert "LLM_API_KEY" in str(exc.value)
    with pytest.raises(ConfigError):
        cfg.check_feishu()
