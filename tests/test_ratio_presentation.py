"""Exact report-output regressions for shared ratio presentation decisions."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from engine import report, report_html
from engine.metrics import Metric
from tests.test_rnd_disclosure import (
    _domestic_no_rnd, _domestic_with_rnd, _fpi_no_rnd, _fpi_with_rnd, _make_res,
)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 13, 12, 0, tzinfo=tz)


CASES = ["gaap", "adjusted", "disabled", "ifrs", "ifrs_disabled", "ifrs_no_rnd",
         "short_history", "missing_adjusted", "missing_ratios", "zero", "unstamped"]


def result_for(case):
    factory = _domestic_no_rnd if case == "gaap" else _domestic_with_rnd
    if case in ("ifrs", "ifrs_disabled"):
        factory = _fpi_with_rnd
    elif case == "ifrs_no_rnd":
        factory = _fpi_no_rnd
    cd = factory()
    if case == "short_history":
        cd.series = {key: facts[-2:] for key, facts in cd.series.items()}
    cfg = {"durability": {"rnd_capitalization": {"enabled": case not in ("disabled", "ifrs_disabled")}}}
    res = _make_res(cd, cfg)
    if case == "missing_adjusted":
        res.ratios.pop("roic_adjusted", None)
    elif case == "missing_ratios":
        res.ratios.pop("roe", None)
        res.ratios["roa"] = Metric("roa", None, '<missing & "quoted">')
        res.ratios["roic_adjusted"] = Metric("roic_adjusted", None, "unused note")
    elif case == "zero":
        res.ratios["roic_adjusted"] = Metric("roic_adjusted", 0.0)
        res.ratios["current_ratio"] = Metric("current_ratio", 0.0)
    elif case == "unstamped":
        res.rnd_regime = None
    return res


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("surface", ["markdown", "html", "fragment"])
def test_exact_ratio_report_output(case, surface, monkeypatch):
    monkeypatch.setattr(report, "datetime", FixedDatetime)
    monkeypatch.setattr(report_html, "datetime", FixedDatetime)
    render = {"markdown": report.render, "html": report_html.render,
              "fragment": report_html.render_fragment}[surface]
    expected = json.loads(Path(__file__).with_name("ratio_report_fingerprints.json").read_text())[case][surface]
    res = result_for(case)
    if case == "unstamped":
        with pytest.raises(ValueError) as exc:
            render(res)
        assert str(exc.value) == expected
    else:
        assert hashlib.sha256(render(res).encode()).hexdigest() == expected
