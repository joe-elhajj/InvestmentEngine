"""Quarterly assembly preserves source identity, missing inputs, and gap order."""
from copy import deepcopy

import pytest

from engine import pipeline as P
from engine.edgar import CompanyData, Fact
from engine.market import Quote

CASES = ['ordinary', 'overlap', 'components', 'missing_debt', 'missing_liquid',
         'zero', 'missing_flows', 'none']


def company(case):
    cd = CompanyData('Q', '1', 'Quarterly', '7372', 'Software')
    cd.unresolved = ['existing gap']
    values = dict(revenue=1000., net_income=100., operating_income=200., cfo=150.,
                  capex=50., long_term_debt=700., short_term_debt=100., cash=40.,
                  short_term_investments=20., long_term_investments=10.,
                  total_assets=2000., total_equity=900.)
    if case == 'none':
        values = {}
    elif case == 'missing_debt':
        del values['short_term_debt']
    elif case == 'missing_liquid':
        for key in ('cash', 'short_term_investments', 'long_term_investments'):
            del values[key]
    elif case == 'zero':
        values = {key: 0. for key in values}
    elif case == 'missing_flows':
        for key in ('revenue', 'cfo', 'capex'):
            del values[key]
    for key, value in values.items():
        concept = 'us-gaap:Test'
        if key == 'long_term_debt':
            concept = 'us-gaap:LongTermDebt' if case == 'overlap' else 'us-gaap:LongTermDebtNoncurrent'
        elif key == 'short_term_debt':
            concept = 'us-gaap:ConvertibleDebtCurrent'
        cd.quarterly[key] = Fact(key, value, '2026-03-31', 2026, concept,
                                 '10-Q', '2026-05-01', accn='quarter-accession')
    if cd.quarterly:
        cd.quarterly['total_assets'].period_end = '2026-04-01'
        cd.quarterly['total_equity'].filed = '2026-05-02'
    return cd


def result(cd):
    return P.derive(cd, Quote('Q', price=10., shares_outstanding=100., market_cap=1000., source='test'), {})


@pytest.mark.parametrize('case', CASES)
def test_quarterly_identity_values_and_gap_order(case, monkeypatch):
    cd = company(case)
    before = deepcopy(cd)
    original = P._debt_total
    calls = []

    def debt(long, short):
        assert long is cd.quarterly.get('long_term_debt')
        assert short is cd.quarterly.get('short_term_debt')
        calls.append((long, short))
        return original(long, short)

    monkeypatch.setattr(P, '_debt_total', debt)
    res = result(cd)
    assert cd == before
    assert res.gaps[0] == 'existing gap'
    if case == 'none':
        assert res.latest_quarter == {}
        assert calls == []
        return
    q = res.latest_quarter
    assert len(calls) == 1
    assert q['facts'] is cd.quarterly
    assert q['period_end'] == '2026-04-01'
    assert q['filed'] == '2026-05-02'
    assert q['total_debt'] == (0. if case == 'zero' else 700. if case in ('overlap', 'missing_debt') else 800.)
    assert q['liquid_assets'] == (None if case == 'missing_liquid' else 0. if case == 'zero' else 70.)
    assert q['fcf'] == (None if case == 'missing_flows' else 0. if case == 'zero' else 100.)
    quarterly_gaps = []
    if case == 'missing_debt':
        quarterly_gaps = ['short_term_debt: no value in latest quarter (2026-04-01)']
    elif case == 'missing_liquid':
        quarterly_gaps = ['cash/short_term_investments/long_term_investments: no value in latest quarter (2026-04-01)']
    start = res.gaps.index('total_debt: no anchor period (total_assets absent); treated as absent, not zero') + 1
    end = res.gaps.index('ev: net_debt unavailable; EV/EBITDA excluded')
    assert res.gaps[start:end] == quarterly_gaps
