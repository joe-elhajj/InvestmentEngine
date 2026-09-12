#!/usr/bin/env python3
"""
analyze.py — CLI entry point.

    python analyze.py AAPL
    python analyze.py AAPL --peers technology
    python analyze.py AAPL --peers MSFT,GOOGL,DELL --scenario base
    python analyze.py AAPL --price 195.0 --shares 15300000000   # offline/reproducible

Outputs a Markdown report to ./reports/<TICKER>_<date>.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from engine.config import load_config as _load_config

from engine.analysis import run_single_ticker
from engine.edgar import EdgarClient
from engine.market import get_quote
from engine.pipeline import derive
from engine import peers as P
from engine import report as R
from engine import report_html as RH


def load_config(path: str) -> dict:
    return _load_config(path)


def resolve_candidates(cfg: dict, peers_arg: str | None) -> list[str]:
    """peers_arg is either a universe name from config or a comma list of tickers."""
    if not peers_arg:
        return []
    universes = cfg.get("peers", {}).get("universes", {})
    if peers_arg in universes:
        return list(universes[peers_arg])
    return [t.strip().upper() for t in peers_arg.split(",") if t.strip()]


def build_peer_table(client, cfg, target_cd, target_size, candidate_tickers, history_years):
    """Fetch each candidate's SIC + size, filter, then score the target against survivors."""
    size_band = cfg.get("peers", {}).get("size_band", {})
    match_level = cfg.get("peers", {}).get("match_sic", "two_digit")
    exclusions = cfg.get("peers", {}).get("exclusions", [])

    candidates, peer_results = [], []
    for tk in candidate_tickers:
        if tk == target_cd.ticker:
            continue
        try:
            cd = client.get_company(tk, history_years)
            q = get_quote(tk)
            res = derive(cd, q, cfg)
            size = q.market_cap or res.derived.get("revenue")
            candidates.append({"ticker": tk, "sic": cd.sic, "size": size})
            peer_results.append(res)
        except Exception as e:  # noqa: BLE001
            print(f"  ! skipping peer {tk}: {e}", file=sys.stderr)

    decisions = P.build_peer_set(target_cd.sic, target_size, candidates,
                                 size_band, match_level, exclusions)
    included = {d.ticker for d in decisions if d.included}
    print("  comp set decisions:")
    for d in decisions:
        mark = "✓" if d.included else "✗"
        print(f"    {mark} {d.ticker}: {d.reason}")

    kept = [r for r in peer_results if r.company.ticker in included]

    metric_keys = ["gross_margin", "operating_margin", "net_margin", "fcf_margin",
                   "roe", "roic", "debt_to_equity"]
    return decisions, kept, metric_keys


def main():
    ap = argparse.ArgumentParser(description="Investment Engine — fundamental analysis")
    ap.add_argument("ticker")
    ap.add_argument("--peers", help="config universe name OR comma-separated tickers")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--price", type=float, help="manual price override (offline)")
    ap.add_argument("--shares", type=float, help="manual shares override (offline)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    sec_cfg = cfg.get("sec", {})
    history_years = cfg.get("report", {}).get("history_years", 15)

    client = EdgarClient(sec_cfg.get("user_agent", ""), sec_cfg.get("request_delay_seconds", 0.2))

    print(f"Fetching {args.ticker} from EDGAR ...")
    res = run_single_ticker(
        args.ticker, cfg, client,
        manual_price=args.price, manual_shares=args.shares,
        history_years=history_years,
    )
    cd = res.company
    quote = res.quote
    target_size = quote.market_cap or res.derived.get("revenue")

    peer_table = None
    candidates = resolve_candidates(cfg, args.peers)
    if candidates:
        print("Building peer set ...")
        decisions, kept, metric_keys = build_peer_table(
            client, cfg, cd, target_size, candidates, history_years)
        peer_table = []
        for key in metric_keys:
            tv = res.ratios.get(key).value if res.ratios.get(key) else None
            pv = [r.ratios.get(key).value for r in kept if r.ratios.get(key)]
            peer_table.append(P.relative_score(key, tv, pv))

    md = R.render(res, peer_table)
    out_dir = Path(cfg.get("report", {}).get("output_dir", "reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{cd.ticker}_{datetime.now():%Y%m%d}.md"
    out_path.write_text(md)
    html_out_path = out_dir / f"{cd.ticker}_{datetime.now():%Y%m%d}.html"
    html_out_path.write_text(RH.render(res, peer_table))
    print(f"\nReport written to {out_path}")
    print(f"Report written to {html_out_path}")
    if res.gaps:
        print(f"Data gaps: {', '.join(res.gaps)}")


if __name__ == "__main__":
    main()
