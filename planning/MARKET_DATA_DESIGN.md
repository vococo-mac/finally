# Market Data Backend — Detailed Implementation Design

Implementation-ready design for the FinAlly market data subsystem. Covers the unified interface, in-memory price cache, GBM simulator, Massive API client, SSE streaming endpoint, and FastAPI lifecycle integration.

Everything in this document lives under `backend/app/market/`. The code here is already implemented — this document serves as the authoritative reference for how it works and how downstream code should integrate with it.

---

## Table of Contents

1. [File Structure & Module Responsibilities](#1-file-structure--module-responsibilities)
2. [Data Model — `models.py`](#2-data-model--modelspy)
3. [Price Cache — `cache.py`](#3-price-cache--cachepy)
4. [Abstract Interface — `interface.py`](#4-abstract-interface--interfacepy)
5. [Seed Prices & Ticker Parameters — `seed_prices.py`](#5-seed-prices--ticker-parameters--seed_pricespy)
6. [GBM Simulator — `simulator.py`](#6-gbm-simulator--simulatorpy)
7. [Massive API Client — `massive_client.py`](#7-massive-api-client--massive_clientpy)
8. [Factory — `factory.py`](#8-factory--factorypy)
9. [SSE Streaming Endpoint — `stream.py`](#9-sse-streaming-endpoint--streampy)
10. [FastAPI Lifecycle Integration](#10-fastapi-lifecycle-integration)
11. [Watchlist Coordination](#11-watchlist-coordination)
12. [Testing Strategy](#12-testing-strategy)
13. [Error Handling & Edge Cases](#13-error-handling--edge-cases)
14. [Configuration Summary](#14-configuration-summary)

---

## 1. File Structure & Module Responsibilities

```
backend/
  app/
    market/
      __init__.py             # Public API re-exports
      models.py               # PriceUpdate frozen dataclass
      cache.py                # PriceCache — thread-safe in-memory price store
      interface.py            # MarketDataSource ABC
      seed_prices.py          # SEED_PRICES, TICKER_PARAMS, correlation constants
      simulator.py            # GBMSimulator (math engine) + SimulatorDataSource
      massive_client.py       # MassiveDataSource (Polygon.io REST poller)
      factory.py              # create_market_data_source() — env-based selection
      stream.py               # SSE endpoint factory (FastAPI router)
  tests/
    market/
      test_models.py          # 11 tests — PriceUpdate properties and serialization
      test_cache.py           # 14 tests — cache operations, version counter, thread safety
      test_simulator.py       # 23 tests — GBMSimulator math, correlations, events
      test_simulator_source.py # 10 tests — SimulatorDataSource async integration
      test_massive.py         # 13 tests — MassiveDataSource with mocked REST client
      test_factory.py         # 8 tests — factory env-var selection logic
```

Each source file has a single responsibility. The `__init__.py` re-exports the public API so downstream code imports from `app.market` without reaching into submodules.

### Data Flow

```
                    ┌───────────────────┐
                    │   Environment     │
                    │   MASSIVE_API_KEY  │
                    └────────┬──────────┘
                             │
                    ┌────────▼──────────┐
                    │    factory.py      │
                    │ create_market_     │
                    │ data_source()      │
                    └────────┬──────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
    ┌─────────▼─────────┐        ┌─────────▼─────────┐
    │ SimulatorDataSource│        │ MassiveDataSource  │
    │ (GBM engine)       │        │ (REST poller)      │
    │ ticks every 500ms  │        │ polls every 15s    │
    └─────────┬─────────┘        └─────────┬─────────┘
              │                             │
              │    cache.update(ticker,     │
              │         price)             │
              └──────────────┬──────────────┘
                             │
                    ┌────────▼──────────┐
                    │    PriceCache      │
                    │ (thread-safe,      │
                    │  version-counted)  │
                    └────────┬──────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
    ┌─────────▼───┐  ┌──────▼──────┐  ┌───▼──────────┐
    │ SSE stream  │  │  Portfolio  │  │    Trade     │
    │ /api/stream │  │  valuation  │  │  execution   │
    │ /prices     │  │             │  │              │
    └─────────────┘  └─────────────┘  └──────────────┘
```

---

## 2. Data Model — `models.py`

`PriceUpdate` is the **only data structure** that leaves the market data layer. Every downstream consumer — SSE streaming, portfolio valuation, trade execution — works exclusively with this type.

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

### Design Decisions

| Choice | Rationale |
|--------|-----------|
| `frozen=True` | Price updates are immutable value objects — safe to share across async tasks without copying |
| `slots=True` | Minor memory optimization; we create many of these per second |
| Computed properties | `change`, `direction`, `change_percent` are derived from `price` and `previous_price` so they can never become inconsistent |
| `to_dict()` | Single serialization point used by both the SSE endpoint and REST API responses |

---

## 3. Price Cache — `cache.py`

The price cache is the **central data hub**. Data sources write to it; SSE streaming and portfolio valuation read from it. It must be thread-safe because the Massive client runs synchronous API calls in `asyncio.to_thread()` while SSE reads happen on the async event loop.

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        Automatically computes direction and change from the previous price.
        If this is the first update for the ticker, previous_price == price (direction='flat').
        """
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get the latest price for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache (e.g., when removed from watchlist)."""
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Current version counter. Useful for SSE change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Why a Version Counter?

The SSE streaming loop polls the cache every ~500ms. Without a version counter, it would serialize and send all prices every tick even if nothing changed (e.g., Massive API only updates every 15s). The version counter lets the SSE loop skip sends when nothing is new:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Why `threading.Lock` Instead of `asyncio.Lock`?

- The Massive client's synchronous `get_snapshot_all()` runs in `asyncio.to_thread()` — a real OS thread. `asyncio.Lock` would not protect against that.
- `threading.Lock` works correctly from both sync threads and the async event loop.
- On CPython with the GIL, lock contention for 10 tickers at 2 updates/sec is negligible.

---

## 4. Abstract Interface — `interface.py`

```python
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        # ... app runs ...
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        # ... app shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that periodically writes to the PriceCache.
        Must be called exactly once. Calling start() twice is undefined behavior.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times. After stop(), the source will not write
        to the cache again.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.

        The next update cycle will include this ticker.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. No-op if not present.

        Also removes the ticker from the PriceCache.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

### Why Push-to-Cache Instead of Return Values?

The push model decouples timing. The simulator ticks at 500ms, Massive polls at 15s, but SSE always reads from the cache at its own 500ms cadence. There is no need for the SSE layer to know which data source is active or what its update interval is.

---

## 5. Seed Prices & Ticker Parameters — `seed_prices.py`

Constants only — no logic, no imports. Shared by the simulator (for initial prices and GBM parameters) and potentially by the Massive client (as fallback prices before the first API response).

```python
"""Seed prices and per-ticker parameters for the market simulator."""

# Realistic starting prices for the default watchlist
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 800.00,
    "META": 500.00,
    "JPM": 195.00,
    "V": 280.00,
    "NFLX": 600.00,
}

# Per-ticker GBM parameters
# sigma: annualized volatility (higher = more price movement)
# mu: annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for tickers not in the list above (dynamically added)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for the simulator's Cholesky decomposition
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR = 0.6       # Tech stocks move together
INTRA_FINANCE_CORR = 0.5    # Finance stocks move together
CROSS_GROUP_CORR = 0.3      # Between sectors
TSLA_CORR = 0.3             # TSLA does its own thing
```

### Parameter Design Rationale

| Ticker | Sigma | Why |
|--------|-------|-----|
| TSLA | 0.50 | Historically the most volatile large-cap; produces dramatic intraday swings |
| NVDA | 0.40 | AI/GPU hype cycle produces high vol; strong drift (0.08) reflects secular growth |
| NFLX | 0.35 | Streaming wars, subscriber surprise moves |
| META | 0.30 | Moderate-high vol from pivot narratives |
| AMZN | 0.28 | Diversified business dampens extremes |
| GOOGL | 0.25 | Large-cap stability with some search/AI noise |
| AAPL | 0.22 | Blue-chip stability, narrow intraday ranges |
| MSFT | 0.20 | Enterprise software predictability |
| JPM | 0.18 | Bank: rate-sensitive but stable |
| V | 0.17 | Payments: most stable in the set |

---

## 6. GBM Simulator — `simulator.py`

This file contains two classes:
- **`GBMSimulator`**: Pure math engine. Stateful — holds current prices and advances them one step at a time.
- **`SimulatorDataSource`**: The `MarketDataSource` implementation that wraps `GBMSimulator` in an async loop and writes to the `PriceCache`.

### 6.1 GBMSimulator — The Math Engine

#### The Math

Stock prices evolve via Geometric Brownian Motion:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

Where:
- `S(t)` = current price
- `mu` = annualized drift (expected return)
- `sigma` = annualized volatility
- `dt` = time step as fraction of a trading year
- `Z` = correlated standard normal random variable

For 500ms updates: `dt = 0.5 / (252 * 6.5 * 3600) ≈ 8.48e-8`. This tiny `dt` produces sub-cent per-tick moves that accumulate naturally.

#### Correlated Moves via Cholesky Decomposition

Real stocks don't move independently. Given a correlation matrix `C`, we compute `L = cholesky(C)`, then for independent standard normals `Z_ind`:

```
Z_correlated = L @ Z_ind
```

This transforms independent random draws into properly correlated ones while preserving the standard normal distribution of each.

#### Implementation

```python
from __future__ import annotations

import asyncio
import logging
import math
import random

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource
from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices."""

    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}.

        This is the hot path — called every 500ms.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        # Generate n independent standard normal draws
        z_independent = np.random.standard_normal(n)

        # Apply Cholesky to get correlated draws
        if self._cholesky is not None:
            z_correlated = self._cholesky @ z_independent
        else:
            z_correlated = z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            # GBM: S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)
            drift = (mu - 0.5 * sigma ** 2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event: ~0.1% chance per tick per ticker
            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign
                logger.debug(
                    "Random event on %s: %.1f%% %s",
                    ticker,
                    shock_magnitude * 100,
                    "up" if shock_sign > 0 else "down",
                )

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the simulation. Rebuilds the correlation matrix."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the simulation. Rebuilds the correlation matrix."""
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        """Current price for a ticker, or None if not tracked."""
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        """Return the current list of tracked tickers."""
        return list(self._tickers)

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add a ticker without rebuilding Cholesky (for batch initialization)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Rebuild the Cholesky decomposition of the ticker correlation matrix."""
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        """Determine correlation between two tickers based on sector grouping."""
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

#### Correlation Matrix Example (Default 10 Tickers)

For the default watchlist, the correlation matrix looks like:

```
       AAPL  GOOGL  MSFT  AMZN  TSLA  NVDA  META  JPM   V    NFLX
AAPL   1.0   0.6    0.6   0.6   0.3   0.6   0.6   0.3   0.3  0.6
GOOGL  0.6   1.0    0.6   0.6   0.3   0.6   0.6   0.3   0.3  0.6
MSFT   0.6   0.6    1.0   0.6   0.3   0.6   0.6   0.3   0.3  0.6
AMZN   0.6   0.6    0.6   1.0   0.3   0.6   0.6   0.3   0.3  0.6
TSLA   0.3   0.3    0.3   0.3   1.0   0.3   0.3   0.3   0.3  0.3
NVDA   0.6   0.6    0.6   0.6   0.3   1.0   0.6   0.3   0.3  0.6
META   0.6   0.6    0.6   0.6   0.3   0.6   1.0   0.3   0.3  0.6
JPM    0.3   0.3    0.3   0.3   0.3   0.3   0.3   1.0   0.5  0.3
V      0.3   0.3    0.3   0.3   0.3   0.3   0.3   0.5   1.0  0.3
NFLX   0.6   0.6    0.6   0.6   0.3   0.6   0.6   0.3   0.3  1.0
```

The Cholesky decomposition `L` of this matrix satisfies `L @ L^T = C`. When we draw independent `Z_ind ~ N(0, I)` and compute `Z = L @ Z_ind`, the resulting `Z` has the correct pairwise correlations.

#### Random Events

Each tick, each ticker has a 0.1% chance of a "shock event" — a sudden 2-5% move in either direction. With 10 tickers at 2 ticks/second, expect approximately one event every 50 seconds. This adds visual drama to the dashboard without destabilizing prices.

### 6.2 SimulatorDataSource — Async Wrapper

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator.

    Runs a background asyncio task that calls GBMSimulator.step() every
    `update_interval` seconds and writes results to the PriceCache.
    """

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(
            tickers=tickers,
            event_probability=self._event_prob,
        )
        # Seed the cache with initial prices so SSE has data immediately
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
            logger.info("Simulator: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        """Core loop: step the simulation, write to cache, sleep."""
        while True:
            try:
                if self._sim:
                    prices = self._sim.step()
                    for ticker, price in prices.items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

### Key Behaviors

- **Immediate seeding**: `start()` populates the cache with seed prices *before* the loop begins. The SSE endpoint has data on its very first tick — no blank-screen delay.
- **Graceful cancellation**: `stop()` cancels the task and awaits it, catching `CancelledError`. Clean shutdown during FastAPI lifespan teardown.
- **Exception resilience**: The loop catches exceptions per-step so a single bad tick doesn't kill the entire data feed.
- **Cache-on-add**: `add_ticker()` seeds the cache immediately so the frontend sees the new ticker right away.

---

## 7. Massive API Client — `massive_client.py`

Polls the Massive (formerly Polygon.io) REST API snapshot endpoint. The synchronous Massive client runs in `asyncio.to_thread()` to avoid blocking the event loop.

```python
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call, then writes results to the PriceCache.

    Rate limits:
      - Free tier: 5 req/min -> poll every 15s (default)
      - Paid tiers: higher limits -> poll every 2-5s
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: Any = None  # Lazy import to avoid hard dependency

    async def start(self, tickers: list[str]) -> None:
        from massive import RESTClient

        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)

        # Immediate first poll so the cache has data right away
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(tickers),
            self._interval,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch snapshots, update cache."""
        if not self._tickers or not self._client:
            return

        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    timestamp = snap.last_trade.timestamp / 1000.0  # ms -> seconds
                    self._cache.update(
                        ticker=snap.ticker,
                        price=price,
                        timestamp=timestamp,
                    )
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s",
                        getattr(snap, "ticker", "???"),
                        e,
                    )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as e:
            logger.error("Massive poll failed: %s", e)

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        from massive.rest.models import SnapshotMarketType

        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Error Handling Philosophy

The poller is intentionally resilient — it never crashes the application:

| Error | Behavior |
|-------|----------|
| **401 Unauthorized** | Logged as error. Poller keeps running; user fixes `.env` and restarts. |
| **429 Rate Limited** | Logged as error. Next poll retries after `poll_interval` seconds. |
| **Network timeout** | Logged as error. Retries automatically on next cycle. |
| **Malformed snapshot** | Individual ticker skipped with warning. Other tickers still processed. |
| **All tickers fail** | Cache retains last-known prices. SSE keeps streaming stale data (better than nothing). |

### Lazy Import Strategy

`from massive import RESTClient` happens inside `start()`, not at module import time. This means:
- The `massive` package is only required when `MASSIVE_API_KEY` is set.
- The simulator path has zero external dependencies beyond `numpy`.

### Key Differences from Simulator

| Aspect | Simulator | Massive |
|--------|-----------|---------|
| Update frequency | 500ms | 15s (free) / 2-5s (paid) |
| Cache-on-add | Immediate (seed price) | Next poll cycle |
| Ticker format | As-provided | Normalized (uppercase, stripped) |
| External dependencies | `numpy` only | `massive` package |
| Network required | No | Yes |
| Works after hours | Yes (simulated) | Returns last traded price |

---

## 8. Factory — `factory.py`

```python
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    - MASSIVE_API_KEY set and non-empty -> MassiveDataSource (real market data)
    - Otherwise -> SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource

        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource

        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

The factory uses lazy imports in both branches — the unused implementation's module is never loaded.

---

## 9. SSE Streaming Endpoint — `stream.py`

The SSE endpoint holds open a long-lived HTTP connection and pushes price updates to the client as `text/event-stream`.

```python
from __future__ import annotations

import asyncio
import json
import logging

from collections.abc import AsyncGenerator
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create the SSE streaming router with a reference to the price cache."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint for live price updates.

        Streams all tracked ticker prices every ~500ms. The client connects
        with EventSource and receives events in the format:

            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, ...}
        """
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE-formatted price events."""
    yield "retry: 1000\n\n"

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()

                if prices:
                    data = {
                        ticker: update.to_dict()
                        for ticker, update in prices.items()
                    }
                    payload = json.dumps(data)
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### SSE Wire Format

Each event the browser receives:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

### Frontend Integration

```javascript
const eventSource = new EventSource('/api/stream/prices');

eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices = { "AAPL": { ticker, price, previous_price, change, change_percent, direction, timestamp }, ... }

    Object.values(prices).forEach(update => {
        updateTickerDisplay(update.ticker, update);
        if (update.direction !== 'flat') {
            flashPriceCell(update.ticker, update.direction);  // green/red CSS animation
        }
        appendToSparkline(update.ticker, update.price);
    });
};

eventSource.onerror = () => {
    // EventSource auto-reconnects using the retry: 1000 directive
    updateConnectionStatus('reconnecting');
};
```

### Why Poll-and-Push Instead of Event-Driven?

The SSE endpoint polls the cache on a fixed interval rather than being notified by the data source. This produces predictable, evenly-spaced updates for the frontend. Regular spacing is important for clean sparkline visualization.

---

## 10. FastAPI Lifecycle Integration

The market data system starts and stops with the FastAPI application using the `lifespan` context manager pattern.

```python
# backend/app/main.py

from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.market import PriceCache, create_market_data_source, MarketDataSource, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of background services."""

    # --- STARTUP ---
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # Load initial tickers from the database watchlist
    initial_tickers = await load_watchlist_tickers()  # reads from SQLite
    await source.start(initial_tickers)

    # Register the SSE streaming router
    stream_router = create_stream_router(price_cache)
    app.include_router(stream_router)

    yield  # App is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# Dependency injection for route handlers
def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### Accessing Market Data from Other Routes

Trade execution, portfolio valuation, and watchlist management access the price cache via FastAPI dependency injection:

```python
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(prefix="/api")


@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(400, f"Price not yet available for {trade.ticker}")
    # ... execute trade at current_price ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    # Insert into database ...
    await source.add_ticker(payload.ticker)
    # ...


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    # Delete from database ...
    await source.remove_ticker(ticker)
    # ...
```

---

## 11. Watchlist Coordination

When the watchlist changes (via REST API or LLM chat action), the market data source must be notified.

### Flow: Adding a Ticker

```
User (or LLM) -> POST /api/watchlist {ticker: "PYPL"}
  -> Insert into watchlist table (SQLite)
  -> await source.add_ticker("PYPL")
      Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
      Massive: appends to ticker list, appears on next poll (~15s delay)
  -> Return success (ticker + current price if available)
```

### Flow: Removing a Ticker

```
User (or LLM) -> DELETE /api/watchlist/PYPL
  -> Delete from watchlist table (SQLite)
  -> await source.remove_ticker("PYPL")
      Simulator: removes from GBMSimulator, rebuilds Cholesky, removes from cache
      Massive: removes from ticker list, removes from cache
  -> Return success
```

### Edge Case: Ticker Has an Open Position

If the user removes a ticker from the watchlist but still holds shares, the ticker should remain in the data source so portfolio valuation stays accurate:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    # Only stop tracking if no open position
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"status": "ok"}
```

---

## 12. Testing Strategy

### Test Summary

| Module | Tests | Coverage | Focus |
|--------|-------|----------|-------|
| `test_models.py` | 11 | 100% | PriceUpdate properties, serialization, edge cases |
| `test_cache.py` | 14 | 100% | CRUD, version counter, thread safety, rounding |
| `test_simulator.py` | 23 | 98% | GBM math, correlations, events, add/remove |
| `test_simulator_source.py` | 10 | integration | Async lifecycle, cache population, ticker management |
| `test_massive.py` | 13 | 56%* | Polling, error handling, ticker normalization |
| `test_factory.py` | 8 | 100% | Env-var selection logic |

*Massive coverage is 56% because real API methods can't run without the `massive` package in CI.

### Example: Testing the GBM Simulator

```python
class TestGBMSimulator:

    def test_step_returns_all_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        result = sim.step()
        assert set(result.keys()) == {"AAPL", "GOOGL"}

    def test_prices_are_positive(self):
        """GBM prices can never go negative (exp() is always positive)."""
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(10_000):
            prices = sim.step()
            assert prices["AAPL"] > 0

    def test_initial_prices_match_seeds(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_cholesky_rebuilds_on_add(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None  # Only 1 ticker
        sim.add_ticker("GOOGL")
        assert sim._cholesky is not None  # Now 2 tickers

    def test_prices_change_over_time(self):
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(1000):
            sim.step()
        assert sim.get_price("AAPL") != SEED_PRICES["AAPL"]
```

### Example: Testing the Massive Client (Mocked)

```python
def _make_snapshot(ticker: str, price: float, timestamp_ms: int) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap


@pytest.mark.asyncio
class TestMassiveDataSource:

    async def test_poll_updates_cache(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

        mock_snapshots = [
            _make_snapshot("AAPL", 190.50, 1707580800000),
            _make_snapshot("GOOGL", 175.25, 1707580800000),
        ]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_api_error_does_not_crash(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # Should not raise

        assert cache.get_price("AAPL") is None
```

### Running Tests

```bash
cd backend
uv run pytest tests/market/ -v           # All market data tests
uv run pytest tests/market/ -v --cov=app/market  # With coverage
```

---

## 13. Error Handling & Edge Cases

### 13.1 Startup: Empty Watchlist

If the database has no watchlist entries, `start()` receives an empty list. Both data sources handle this gracefully — the simulator produces no prices, the Massive poller skips its API call. The SSE endpoint sends empty events. When the user adds a ticker, the source starts tracking it immediately.

### 13.2 Price Cache Miss During Trade

If a user tries to trade a ticker that has no cached price (just added, Massive hasn't polled yet):

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
    )
```

The simulator avoids this by seeding the cache in `add_ticker()`. The Massive client may have a brief gap until the next poll.

### 13.3 Massive API Key Invalid

If the API key is invalid, the first poll fails with a 401. The poller logs the error and keeps retrying. The SSE endpoint streams empty data. The fix is to correct the API key and restart.

### 13.4 Thread Safety Under Load

The `PriceCache` uses `threading.Lock` (a mutex). Under normal load (10 tickers, 2 updates/sec), lock contention is negligible. The critical section is tiny (dict lookup + assignment). If this ever became a bottleneck with hundreds of tickers, a `ReadWriteLock` would be the fix — but unnecessary for this project.

### 13.5 Simulator Precision

GBM with tiny `dt` produces very small per-tick moves. Floating-point precision is not a concern:
- Prices are `round()`ed to 2 decimal places in `GBMSimulator.step()`
- The exponential formulation `exp(drift + diffusion)` is numerically stable
- Prices are always positive (exponential function can't produce zero or negative)

### 13.6 Cholesky Rebuild Cost

`_rebuild_cholesky()` is O(n^2) for building the correlation matrix and O(n^3) for the decomposition. With n < 50 tickers, this takes microseconds. It only runs when tickers are added or removed — not on every step.

---

## 14. Configuration Summary

All tunable parameters and their defaults:

| Parameter | Location | Default | Description |
|-----------|----------|---------|-------------|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set, use Massive API; otherwise use simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Time between simulator ticks |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Time between Massive API polls |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of random shock per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step (fraction of a trading year) |
| SSE push interval | `_generate_events()` | `0.5` s | Time between SSE pushes to client |
| SSE retry directive | `_generate_events()` | `1000` ms | Browser EventSource reconnection delay |

### Package `__init__.py` — Public API

```python
"""Market data subsystem for FinAlly."""

from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
]
```

### Quick-Start Usage for Downstream Code

```python
from app.market import PriceCache, create_market_data_source

# Startup
cache = PriceCache()
source = create_market_data_source(cache)  # Reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", "MSFT", ...])

# Read prices (from any route handler)
update = cache.get("AAPL")          # PriceUpdate or None
price = cache.get_price("AAPL")     # float or None
all_prices = cache.get_all()        # dict[str, PriceUpdate]

# Dynamic watchlist management
await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")

# Shutdown
await source.stop()
```
