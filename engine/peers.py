"""
peers.py — you own the comp set, not a data vendor.

A peer comparison is only as good as the peer set. So rather than trusting a
provider's "industry" bucket, the analyst supplies a *candidate universe*
(in config.yaml) and the engine enforces explicit discipline on it:

  - SIC family match (2-digit by default) against the target
  - a size band (target's market cap or revenue must be within a multiple range)
  - an explicit exclusion list

Every inclusion/exclusion decision is recorded with a reason, so the final
comp set is defensible and reproducible. That is the line between a real
analyst's tool and a screener.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional


@dataclass
class PeerDecision:
    ticker: str
    included: bool
    reason: str
    sic: str = ""
    size: Optional[float] = None


def sic_family(sic: str, level: str = "two_digit") -> str:
    sic = (sic or "").strip()
    if not sic:
        return ""
    if level == "two_digit":
        return sic[:2]
    if level == "three_digit":
        return sic[:3]
    return sic  # exact


def build_peer_set(
    target_sic: str,
    target_size: Optional[float],
    candidates: list[dict],   # each: {ticker, sic, size}
    size_band: dict,
    match_level: str = "two_digit",
    exclusions: Optional[list[str]] = None,
) -> list[PeerDecision]:
    exclusions = {t.upper() for t in (exclusions or [])}
    target_fam = sic_family(target_sic, match_level)
    lo = size_band.get("lower_multiple", 0.25)
    hi = size_band.get("upper_multiple", 4.0)

    decisions: list[PeerDecision] = []
    for c in candidates:
        tk = c["ticker"].upper()
        sic = str(c.get("sic", ""))
        size = c.get("size")

        if tk in exclusions:
            decisions.append(PeerDecision(tk, False, "explicitly excluded", sic, size))
            continue
        if target_fam and sic_family(sic, match_level) != target_fam:
            decisions.append(PeerDecision(
                tk, False, f"SIC family {sic_family(sic, match_level)} != target {target_fam}", sic, size))
            continue
        if target_size and size:
            ratio = size / target_size
            if not (lo <= ratio <= hi):
                decisions.append(PeerDecision(
                    tk, False, f"size {ratio:.2f}x target outside [{lo}, {hi}]", sic, size))
                continue
        decisions.append(PeerDecision(tk, True, "passes SIC + size filter", sic, size))
    return decisions


@dataclass
class RelativeScore:
    metric: str
    target: Optional[float]
    peer_median: Optional[float]
    percentile: Optional[float]   # target's percentile rank within peers (0-100)
    zscore: Optional[float]
    n_peers: int


def relative_score(metric_name: str, target_value: Optional[float],
                   peer_values: list[float]) -> RelativeScore:
    peer_values = [v for v in peer_values if v is not None]
    n = len(peer_values)
    if n == 0:
        return RelativeScore(metric_name, target_value, None, None, None, 0)
    median = statistics.median(peer_values)
    pct = z = None
    if target_value is not None:
        below = sum(1 for v in peer_values if v < target_value)
        pct = 100.0 * below / n
        if n >= 2:
            sd = statistics.pstdev(peer_values)
            mean = statistics.fmean(peer_values)
            z = (target_value - mean) / sd if sd > 0 else 0.0
    return RelativeScore(metric_name, target_value, median, pct, z, n)
