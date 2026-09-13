"""Shared offline app configuration; public SEC placeholders stay invalid."""
import pytest
import yaml


@pytest.fixture
def app_test_config(tmp_path, monkeypatch):
    from app import main
    from engine.config import load_config

    cfg = load_config(main.CONFIG_PATH)
    cfg['sec']['user_agent'] = 'Investment Engine Tests - tests@example.org'
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(main, 'CONFIG_PATH', path)
    return cfg
