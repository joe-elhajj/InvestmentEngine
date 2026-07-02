"""
universe.py — reference population for peer-relative percentile scoring.

The S&P 500 constituent list (config/sp500_universe.txt) is the canonical,
analyst-owned source of truth.  Universe metric distributions are pre-computed
once per quarter and disk-cached, keyed by (universe_version, config_hash), so
they are not recomputed on every scoring call.

Runtime code NEVER fetches the ticker list from the network; it reads the
committed file.  The universe build (build_distribution) calls EDGAR using the
existing company-facts disk cache, so most data is already local.

Tests inject synthetic UniverseDistribution objects; the universe build never
runs in CI.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_DEFAULT_UNIVERSE_FILE = Path("config/sp500_universe.txt")
_DEFAULT_CACHE_DIR = Path(".cache/universe")


# ---------------------------------------------------------------------------
# Data structure
# ---------------------------------------------------------------------------

@dataclass
class UniverseDistribution:
    """Pre-computed per-metric value distributions from the reference universe."""
    version: str
    config_hash: str
    # metric_name -> sorted list of values across all universe tickers
    data: dict[str, list[float]] = field(default_factory=dict)

    def get(self, metric: str) -> list[float]:
        """Return the distribution for a metric, or [] if not present."""
        return self.data.get(metric, [])


# ---------------------------------------------------------------------------
# Universe file
# ---------------------------------------------------------------------------

def load_tickers(universe_file: Path = _DEFAULT_UNIVERSE_FILE) -> list[str]:
    """Load tickers from the universe file, skipping blank lines and comments."""
    if not universe_file.exists():
        log.warning("Universe file %s not found; returning empty ticker list", universe_file)
        return []
    tickers = []
    for line in universe_file.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            tickers.append(stripped)
    return tickers


# ---------------------------------------------------------------------------
# Distribution cache (disk)
# ---------------------------------------------------------------------------

def _cache_path(cache_dir: Path, version: str, config_hash: str) -> Path:
    key = f"{version}|{config_hash}"
    slug = hashlib.sha256(key.encode()).hexdigest()[:16]
    return cache_dir / f"universe_dist_{slug}.json"


def load_cached_distribution(
    version: str,
    config_hash: str,
    cache_dir: Path = _DEFAULT_CACHE_DIR,
) -> Optional[UniverseDistribution]:
    """Return a previously-built distribution from disk, or None if absent/stale."""
    path = _cache_path(cache_dir, version, config_hash)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        if raw.get("version") != version or raw.get("config_hash") != config_hash:
            log.debug("Universe cache %s: version/hash mismatch; ignoring", path)
            return None
        return UniverseDistribution(
            version=raw["version"],
            config_hash=raw["config_hash"],
            data={k: list(v) for k, v in raw["data"].items()},
        )
    except Exception as e:
        log.warning("Failed to read universe cache %s: %s", path, e)
        return None


def save_distribution(
    dist: UniverseDistribution,
    cache_dir: Path = _DEFAULT_CACHE_DIR,
) -> None:
    """Persist a distribution to disk for future runs."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, dist.version, dist.config_hash)
    path.write_text(json.dumps(
        {"version": dist.version, "config_hash": dist.config_hash, "data": dist.data},
        indent=2,
    ))
    log.info("Universe distribution cached at %s", path)


# ---------------------------------------------------------------------------
# Distribution builder
# ---------------------------------------------------------------------------

def build_distribution(
    tickers: list[str],
    edgar_client,          # EdgarClient — uses its own disk cache
    config: dict,
    version: str,
    config_hash: str,
    history_years: int = 15,
) -> UniverseDistribution:
    """
    Compute per-metric distributions across the universe by fetching EDGAR data
    (from cache when available) for each ticker.

    Metrics collected:
      - gross_margin          (latest annual)
      - capex_revenue_ratio   (latest annual)
      - rnd_revenue_ratio     (latest annual)

    Failures per ticker are logged and skipped; one bad ticker never kills the build.
    """
    from engine.pipeline import derive_annual_series

    gross_margins: list[float] = []
    capex_revenue: list[float] = []
    rnd_revenue: list[float] = []
    skipped = 0

    for tk in tickers:
        try:
            cd = edgar_client.get_company(tk, history_years)
            annual = derive_annual_series(cd, config)
            if not annual:
                continue
            yd = annual[max(annual)]
            if yd.gross_margin is not None:
                gross_margins.append(yd.gross_margin)
            if yd.capex is not None and yd.revenue is not None and yd.revenue > 0:
                capex_revenue.append(yd.capex / yd.revenue)
            if yd.rnd is not None and yd.revenue is not None and yd.revenue > 0:
                rnd_revenue.append(yd.rnd / yd.revenue)
        except Exception as e:
            log.warning("Universe build: skipping %s — %s", tk, e)
            skipped += 1

    log.info(
        "Universe build complete: %d tickers processed, %d skipped",
        len(tickers) - skipped, skipped,
    )
    return UniverseDistribution(
        version=version,
        config_hash=config_hash,
        data={
            "gross_margin": sorted(gross_margins),
            "capex_revenue_ratio": sorted(capex_revenue),
            "rnd_revenue_ratio": sorted(rnd_revenue),
        },
    )


# ---------------------------------------------------------------------------
# Convenience: load-or-build
# ---------------------------------------------------------------------------

def get_or_build_distribution(
    config: dict,
    config_hash: str,
    edgar_client,
    universe_file: Optional[Path] = None,
    cache_dir: Path = _DEFAULT_CACHE_DIR,
    history_years: int = 15,
) -> UniverseDistribution:
    """
    Load the cached distribution for (universe_version, config_hash).
    If not cached, build it from EDGAR data and save.

    This is the entry point for callers; tests skip this entirely and inject
    synthetic UniverseDistribution objects directly.
    """
    version = config.get("universe", {}).get("version", "unversioned")
    cached = load_cached_distribution(version, config_hash, cache_dir)
    if cached is not None:
        log.info("Universe distribution loaded from cache (version=%s, hash=%s)", version, config_hash)
        return cached

    uf = universe_file or Path(config.get("universe", {}).get("file", str(_DEFAULT_UNIVERSE_FILE)))
    tickers = load_tickers(uf)
    log.info("Building universe distribution from %d tickers (version=%s)", len(tickers), version)
    dist = build_distribution(tickers, edgar_client, config, version, config_hash, history_years)
    save_distribution(dist, cache_dir)
    return dist
