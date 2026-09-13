"""Shared defaults preserve omitted-config behavior and explicit overrides."""
from copy import deepcopy

from engine import durability as D, pipeline as P, valuation as V
from tests.test_rnd_disclosure import _domestic_with_rnd, _make_res


def configurations():
    scenarios = {name: {"wacc": w, "terminal_growth": 0.025, "fcf_growth": g}
                 for name, w, g in [("bear", 0.11, 0.02), ("base", 0.09, 0.05), ("bull", 0.07, 0.08)]}
    omitted = {"valuation": {"dcf": {"scenarios": scenarios}},
               "durability": {"rnd_capitalization": {"enabled": True}}}
    explicit = deepcopy(omitted)
    explicit["valuation"].update(assumed_tax_rate=0.21, min_history_years=4)
    explicit["valuation"]["dcf"]["projection_years"] = 5
    explicit["durability"]["rnd_capitalization"]["amortization_years"] = 5
    override = deepcopy(explicit)
    override["valuation"].update(assumed_tax_rate=0.3, min_history_years=6)
    override["valuation"]["dcf"]["projection_years"] = 7
    override["durability"]["rnd_capitalization"]["amortization_years"] = 3
    return [{}, {"valuation": {}, "durability": {}}, omitted, explicit, override]


def outcomes(cfg):
    res = _make_res(_domestic_with_rnd(), cfg)
    resolved = D._resolve_config(cfg)
    return res, resolved, D._config_hash(resolved), D.score(res, cfg)


def test_omitted_and_explicit_defaults_match():
    configs = configurations()
    assert outcomes(configs[0]) == outcomes(configs[1])
    assert outcomes(configs[2]) == outcomes(configs[3])
    resolved = D._resolve_config({})
    assert resolved["valuation"] == {"assumed_tax_rate": 0.21, "min_history_years": 4}
    assert resolved["rnd_capitalization"] == {"enabled": False, "amortization_years": 5}


def test_overrides_reach_forward_reverse_dcf_and_rnd():
    res, resolved, fingerprint, score = outcomes(configurations()[4])
    assert resolved["valuation"] == {"assumed_tax_rate": 0.3, "min_history_years": 6}
    assert resolved["rnd_capitalization"]["amortization_years"] == 3
    assert score.config_hash == fingerprint
    assert all(result.assumptions["projection_years"] == 7 for result in res.dcf.values())
    assert res.implied_growth_result.assumptions["projection_years"] == 7
    latest = res.annual_series[max(res.annual_series)]
    assert latest.nopat == latest.operating_income * (1 - 0.3)
    assert latest.research_asset is not None
    assert P._build_year_entry(res.company, latest.period_end, 0.3, 3) == latest
