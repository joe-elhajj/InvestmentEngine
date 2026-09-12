"""F-10: DCF structure is rejected before company-data gating or arithmetic."""
import copy
import re
from pathlib import Path

import pytest
import yaml

from engine.config import load_config, validate_dcf_config
from engine import valuation as V
from engine import pipeline as P
from tests.test_core import _fake_company
from engine.market import Quote


def _config():
    return yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())


@pytest.mark.parametrize("path,value", [
    (("valuation", "dcf"), None),
    (("valuation", "dcf"), []),
    (("valuation", "dcf", "scenarios"), {}),
    (("valuation", "dcf", "scenarios"), []),
    (("valuation", "dcf", "scenarios"), None),
    (("valuation", "dcf", "scenarios", "bear"), None),
    (("valuation", "dcf", "scenarios", "base"), []),
    (("valuation", "dcf", "scenarios", "base", "wacc"), "0.09"),
    (("valuation", "dcf", "scenarios", "base", "wacc"), True),
    (("valuation", "dcf", "scenarios", "base", "wacc"), float("nan")),
    (("valuation", "dcf", "scenarios", "base", "terminal_growth"), float("inf")),
    (("valuation", "dcf", "scenarios", "base", "fcf_growth"), []),
    (("valuation", "dcf", "scenarios", "base", "fcf_growth"), None),
    (("valuation", "dcf", "scenarios", "base", "fcf_growth"), [0.1, float("-inf")]),
    (("valuation", "dcf", "scenarios", "base", "fcf_growth"), [False]),
    (("valuation", "dcf", "scenarios", "base", "typo"), 0.1),
    (("valuation", "dcf", "scenarios", "typo"), {}),
    (("valuation", "dcf", "projection_years"), 2.5),
    (("valuation", "dcf", "projection_years"), True),
    (("valuation", "dcf", "projection_years"), 0),
    (("valuation", "dcf", "sensitivity"), None),
    (("valuation", "dcf", "sensitivity", "wacc"), [float("nan")]),
])
def test_malformed_config_identifies_field(path, value):
    cfg = _config()
    target = cfg
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match=re.escape(".".join(path[:-1]))):
        validate_dcf_config(cfg)


@pytest.mark.parametrize("field", ["wacc", "terminal_growth", "fcf_growth"])
def test_required_scenario_field(field):
    cfg = _config()
    del cfg["valuation"]["dcf"]["scenarios"]["base"][field]
    with pytest.raises(ValueError, match=field):
        validate_dcf_config(cfg)


@pytest.mark.parametrize("name", ["bear", "base", "bull"])
def test_missing_bundle_rejected_at_every_engine_entry_before_data_checks(name):
    cfg = _config()
    del cfg["valuation"]["dcf"]["scenarios"][name]
    for call in (
        lambda: P.derive(None, None, cfg),
        lambda: V.implied_growth(None, None, None, None, cfg),
        lambda: V.expectations_gap_band(None, None, None, None, None, cfg),
    ):
        with pytest.raises(ValueError, match=name):
            call()


@pytest.mark.parametrize("fragment,field", [
    ("valuation:\n  dcf: {}\n  dcf: {}\n", "valuation.dcf"),
    ("valuation:\n  dcf:\n    scenarios: {}\n    scenarios: {}\n", "valuation.dcf.scenarios"),
    ("valuation:\n  dcf:\n    scenarios:\n      base: {}\n      base: {}\n", "valuation.dcf.scenarios.base"),
    ("valuation:\n  dcf:\n    scenarios:\n      base:\n        wacc: 0.1\n        wacc: 0.2\n", "valuation.dcf.scenarios.base.wacc"),
])
def test_duplicate_yaml_keys_fail_in_loader_and_cli(tmp_path, fragment, field):
    import analyze
    path = tmp_path / "config.yaml"
    path.write_text(fragment)
    for loader in (load_config, analyze.load_config):
        with pytest.raises(ValueError, match=re.escape(field) + ": duplicate YAML key"):
            loader(path)


def test_app_startup_uses_same_validation(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    cfg = _config()
    del cfg["valuation"]["dcf"]["scenarios"]["bear"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(main, "CONFIG_PATH", path)
    with pytest.raises(ValueError, match="bear"):
        with TestClient(main.app):
            pass


@pytest.mark.parametrize("growth", [0.1, [0.1], [0.1] * 7])
def test_valid_config_outputs_unchanged(growth, monkeypatch, tmp_path):
    cfg = _config()
    cfg["valuation"]["dcf"]["scenarios"]["base"]["fcf_growth"] = growth
    del cfg["valuation"]["dcf"]["projection_years"]  # existing five-year default
    original = copy.deepcopy(cfg)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    assert load_config(path) == cfg
    quote = Quote("TEST", price=50, shares_outstanding=100, market_cap=5000, source="test")
    actual = P.derive(_fake_company(), quote, cfg)
    assert set(actual.dcf) == {"bear", "base", "bull"}
    assert cfg == original
    monkeypatch.setattr(P, "validate_dcf_config", lambda *a, **k: None)
    monkeypatch.setattr(V, "validate_dcf_config", lambda *a, **k: None)
    previous = P.derive(_fake_company(), quote, cfg)
    assert actual == previous


def test_repository_config_and_existing_optional_modes():
    assert load_config(Path(__file__).resolve().parents[1] / "config.yaml") == _config()
    for cfg in ({}, {"valuation": {}}, {"valuation": {"dcf": {}}}):
        validate_dcf_config(cfg)
    cfg = _config()
    for sc in cfg["valuation"]["dcf"]["scenarios"].values():
        del sc["fcf_growth"]
        sc["wacc"] = -0.1  # no new economic range or WACC/terminal-growth rule
    validate_dcf_config(cfg, require_growth=False)


def test_recursive_yaml_is_rejected_clearly(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("valuation:\n  dcf: &dcf\n    scenarios: *dcf\n")
    with pytest.raises(ValueError, match="valuation.dcf.scenarios: recursive YAML mapping"):
        load_config(path)
