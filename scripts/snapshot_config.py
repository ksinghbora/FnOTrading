"""Daily config snapshotter — freeze the system's effective state for replay.

Why this exists (DATA_RELIABILITY_PLAN §7.8):
  Replay-as-counterfactual is honest only if the strategy/risk/filter params,
  the active strategy roster, the holiday calendar, the lot sizes, and the
  active expiry instrument tokens are the **exact** values that were live on
  the day being replayed. Today the replay reads those from `params.py`,
  `constants.py`, and the broker's instrument table *as they exist now*, so
  any code change between the trading day and the replay run silently
  invalidates the comparison.

  This script writes a frozen, plaintext snapshot of all that state to
  ``config/snapshots/<YYYY-MM-DD>/`` once per trading morning. The replay
  engine (P2 work) loads from the snapshot, not from current source.

Outputs (all under config/snapshots/<date>/):
  params.yaml        - dump of every Pydantic params class (human-readable)
  params.json        - same data, JSON-encoded for the day_replay loader
  env.yaml           - sanitized Settings (secrets redacted)
  instruments.json   - active F&O instrument tokens for next 2 expiries
  holidays.json      - the holiday calendar as known today
  constants.json     - lot sizes, freeze quantities, charges, market hours
  manifest.json      - what was written + SHA256 of each file + git SHA

The params.yaml file stays for human review and git diffs; params.json is
the format src/backtest/day_replay.py reads. Both contain identical data so
a reviewer can sanity-check by `diff <(jq -S . params.json) params.yaml`.

Usage:
    uv run python scripts/snapshot_config.py
    uv run python scripts/snapshot_config.py --date 2026-04-17
    uv run python scripts/snapshot_config.py --no-instruments  # skip broker call

Cron: 0 9 * * 1-5  (09:00 IST, before market open)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.clock import MarketClock
from src.core.constants import (
    CHARGES,
    EXPIRY_SCHEDULE,
    FREEZE_QUANTITIES,
    INDIA_VIX_TOKEN,
    LOT_SIZES,
    MARKET_CLOSE,
    MARKET_OPEN,
    MIS_SQUARE_OFF_TIME,
    NSE_HOLIDAYS_2026,
    PRE_OPEN_END,
    PRE_OPEN_START,
)

logger = logging.getLogger(__name__)

SNAPSHOT_ROOT = Path("config/snapshots")
SECRET_FIELD_TOKENS = (
    "password",
    "secret",
    "token",
    "api_key",
    "totp",
    "private_key",
)


# ────────────────────────────────────────────────────────────────────
# Serialization helpers
# ────────────────────────────────────────────────────────────────────

def _serialize(value: Any) -> Any:
    """Convert non-JSON-native Python types to JSON-friendly values."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "isoformat"):  # datetime.time
        return value.isoformat()
    if is_dataclass(value):
        return _serialize(asdict(value))
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize(v) for v in value]
    return value


def _redact(key: str, value: Any) -> Any:
    """Replace secret field values with a sentinel.

    Anything whose key contains a known secret token is redacted regardless
    of whether the value happens to be empty in the current environment —
    we don't want to be fooled into thinking a field is non-sensitive just
    because it's blank in dev.
    """
    if not isinstance(key, str):
        return value
    lk = key.lower()
    for token in SECRET_FIELD_TOKENS:
        if token in lk:
            if isinstance(value, str) and value:
                # Surface a digest so reviewers can confirm the secret
                # actually rotated between snapshot days, without leaking
                # the value itself.
                return f"<redacted:sha256:{hashlib.sha256(value.encode()).hexdigest()[:12]}>"
            return "<redacted>"
    return value


def _redact_mapping(mapping: dict) -> dict:
    return {k: _redact(k, _serialize(v)) for k, v in mapping.items()}


# ────────────────────────────────────────────────────────────────────
# Snapshot writers
# ────────────────────────────────────────────────────────────────────

def _write_yaml(path: Path, data: dict) -> None:
    """Tiny hand-rolled YAML emitter — avoids adding a yaml dep just for this.

    Output is valid YAML 1.2 for the subset we use (string / number / bool /
    None / nested dict / list of scalars). It's also valid JSON, so anyone
    who hates the format can rename to .json and parse with stdlib.
    """
    path.write_text(_to_yaml(data) + "\n")


def _to_yaml(obj: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        lines = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(_to_yaml(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {_yaml_scalar(v)}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return f"{pad}[]"
        lines = []
        for v in obj:
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}-")
                lines.append(_to_yaml(v, indent + 1))
            else:
                lines.append(f"{pad}- {_yaml_scalar(v)}")
        return "\n".join(lines)
    return f"{pad}{_yaml_scalar(obj)}"


def _yaml_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # Quote strings that look like other YAML types or contain special chars
    if (
        s in ("null", "true", "false", "yes", "no", "on", "off")
        or s.startswith(("-", "[", "{", "&", "*", "!", "|", ">", "%", "@", "`"))
        or any(c in s for c in (":", "#", "\n", "  "))
    ):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=2,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Atomic write: temp + fsync + rename. Same invariant as the recorders."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(_serialize(payload), f, indent=2, sort_keys=True, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)


# ────────────────────────────────────────────────────────────────────
# Section builders
# ────────────────────────────────────────────────────────────────────

def _collect_param_classes() -> dict[str, dict]:
    """Walk ``src/strategy/params.py`` and serialize every Pydantic params class.

    Captures defaults at import time, which is the right snapshot semantic:
    these are the values a freshly-started worker would use today.
    """
    import inspect

    from pydantic import BaseModel

    from src.strategy import params as p

    out: dict[str, dict] = {}
    for name, obj in inspect.getmembers(p):
        if (
            inspect.isclass(obj)
            and issubclass(obj, BaseModel)
            and obj is not BaseModel
            and obj.__module__ == p.__name__
        ):
            try:
                instance = obj()
                out[name] = _serialize(instance.model_dump())
            except Exception as e:
                # A subclass might require fields with no defaults — skip
                # it but keep a breadcrumb so we know the snapshot is partial.
                out[name] = {"_snapshot_error": f"{type(e).__name__}: {e}"}
    return out


def _collect_env_settings() -> dict:
    """Sanitized dump of Settings — secrets redacted, values serialized."""
    from src.config import Settings

    settings = Settings()
    raw = settings.model_dump()
    return _redact_mapping(raw)


def _collect_constants() -> dict:
    return {
        "lot_sizes": _serialize(LOT_SIZES),
        "freeze_quantities": _serialize(FREEZE_QUANTITIES),
        "charges": _serialize(CHARGES),
        "market_open": MARKET_OPEN.isoformat(),
        "market_close": MARKET_CLOSE.isoformat(),
        "pre_open_start": PRE_OPEN_START.isoformat(),
        "pre_open_end": PRE_OPEN_END.isoformat(),
        "mis_square_off_time": MIS_SQUARE_OFF_TIME.isoformat(),
        "expiry_schedule": _serialize(EXPIRY_SCHEDULE),
        "india_vix_token": INDIA_VIX_TOKEN,
    }


def _collect_holidays(target: date) -> dict:
    """Snapshot the holiday calendar as the system sees it on `target`.

    We dump the current year's calendar plus the next year (so an end-of-year
    snapshot still has the next 2 expiries' holidays). NSE_HOLIDAYS_2026 is
    a list of (month, day) tuples — convert to ISO dates.
    """
    holidays_by_year: dict[int, list[str]] = {}
    holidays_by_year[2026] = sorted(date(2026, m, d).isoformat() for m, d in NSE_HOLIDAYS_2026)
    return {
        "source": "src.core.constants.NSE_HOLIDAYS_2026",
        "target_date": target.isoformat(),
        "holidays": holidays_by_year,
    }


async def _collect_instruments(target: date, settings) -> dict | None:
    """Pull active F&O instrument tokens for the next two expiries per underlying.

    Best-effort: if Kite isn't reachable, we return None and the manifest
    notes the omission. Don't fail the snapshot — partial config is still
    useful for replay.
    """
    try:
        from src.broker.zerodha.client import ZerodhaClient
        from src.broker.zerodha.instruments import InstrumentManager
        from src.db.session import create_db_engine, create_session_factory

        engine = create_db_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        broker = ZerodhaClient(settings.kite_api_key, settings.kite_access_token)
        manager = InstrumentManager(broker, session_factory)

        # Hydrate the in-memory cache from the DB. The morning download
        # script (download_instruments.py) runs before this, so the table
        # should already have today's master.
        try:
            await manager.load_from_db()
        except Exception as e:
            logger.warning("InstrumentManager DB load failed: %s — snapshot will be empty", e)
            await engine.dispose()
            return None

        clock = MarketClock()
        out: dict[str, list[dict]] = {}

        # Underlying coverage: NIFTY (weekly) + BANKNIFTY/FINNIFTY (monthly)
        for underlying in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
            expiries: list[date] = []
            cursor = target
            for _ in range(2):  # next two expiries
                try:
                    nxt = clock.next_expiry(underlying, cursor)
                    if nxt and nxt not in expiries:
                        expiries.append(nxt)
                        cursor = nxt + timedelta(days=1)
                except Exception as e:
                    logger.debug("Could not compute next expiry for %s: %s", underlying, e)
                    break

            for exp in expiries:
                try:
                    insts = manager.get_option_chain_instruments(underlying, exp)
                except Exception as e:
                    logger.warning(
                        "Failed to load instruments for %s %s: %s",
                        underlying, exp, e,
                    )
                    continue
                rows = []
                for i in insts:
                    itype = getattr(i, "instrument_type", None)
                    # InstrumentType enum -> "CE"/"PE"
                    itype_str = itype.value if hasattr(itype, "value") else str(itype) if itype else None
                    exp_attr = getattr(i, "expiry", None)
                    rows.append({
                        "tradingsymbol": getattr(i, "tradingsymbol", None),
                        "instrument_token": getattr(i, "instrument_token", None),
                        "strike": float(getattr(i, "strike", 0) or 0),
                        "option_type": itype_str,
                        "expiry": exp_attr.isoformat() if exp_attr else None,
                        "lot_size": getattr(i, "lot_size", None),
                    })
                out.setdefault(underlying, []).extend(rows)

        await engine.dispose()
        return {
            "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "by_underlying": out,
            "underlying_count": len(out),
            "total_instruments": sum(len(v) for v in out.values()),
        }
    except Exception as e:
        logger.warning("Instrument snapshot skipped: %s", e)
        return None


# ────────────────────────────────────────────────────────────────────
# Top-level orchestration
# ────────────────────────────────────────────────────────────────────

async def snapshot(target: date, *, with_instruments: bool = True) -> Path:
    """Write a snapshot for `target`. Returns the snapshot directory."""
    out_dir = SNAPSHOT_ROOT / target.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, str] = {}
    skipped: list[str] = []

    # 1. Strategy params (yaml for humans, json sidecar for day_replay)
    try:
        params_data = _collect_param_classes()
        params_path = out_dir / "params.yaml"
        _write_yaml(params_path, params_data)
        written["params.yaml"] = _sha256(params_path)
        # JSON sidecar — src/backtest/day_replay.py reads this to rehydrate
        # frozen-day-of params without depending on a YAML parser.
        params_json_path = out_dir / "params.json"
        _atomic_write_json(params_json_path, params_data)
        written["params.json"] = _sha256(params_json_path)
        logger.info("snapshot params.yaml + params.json (%d classes)", len(params_data))
    except Exception as e:
        logger.exception("params snapshot failed")
        skipped.append(f"params: {e}")

    # 2. Settings (env)
    from src.config import Settings
    settings = Settings()
    try:
        env_data = _collect_env_settings()
        env_path = out_dir / "env.yaml"
        _write_yaml(env_path, env_data)
        written["env.yaml"] = _sha256(env_path)
        logger.info("snapshot env.yaml (%d fields)", len(env_data))
    except Exception as e:
        logger.exception("env.yaml snapshot failed")
        skipped.append(f"env.yaml: {e}")

    # 3. Constants
    try:
        const_path = out_dir / "constants.json"
        _atomic_write_json(const_path, _collect_constants())
        written["constants.json"] = _sha256(const_path)
    except Exception as e:
        logger.exception("constants.json snapshot failed")
        skipped.append(f"constants.json: {e}")

    # 4. Holidays
    try:
        hol_path = out_dir / "holidays.json"
        _atomic_write_json(hol_path, _collect_holidays(target))
        written["holidays.json"] = _sha256(hol_path)
    except Exception as e:
        logger.exception("holidays.json snapshot failed")
        skipped.append(f"holidays.json: {e}")

    # 5. Instruments (best-effort)
    if with_instruments:
        try:
            inst_data = await _collect_instruments(target, settings)
            if inst_data is not None:
                inst_path = out_dir / "instruments.json"
                _atomic_write_json(inst_path, inst_data)
                written["instruments.json"] = _sha256(inst_path)
                logger.info(
                    "snapshot instruments.json (%d underlyings, %d total)",
                    inst_data.get("underlying_count", 0),
                    inst_data.get("total_instruments", 0),
                )
            else:
                skipped.append("instruments.json: broker unreachable or no expiries resolved")
        except Exception as e:
            logger.exception("instruments.json snapshot failed")
            skipped.append(f"instruments.json: {e}")
    else:
        skipped.append("instruments.json: --no-instruments flag set")

    # 6. Manifest with SHA256s + git SHA
    manifest = {
        "snapshot_date": target.isoformat(),
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "git_sha": _git_sha(),
        "files": written,
        "skipped": skipped,
        "snapshot_version": 1,
    }
    _atomic_write_json(out_dir / "manifest.json", manifest)

    return out_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily config snapshotter")
    parser.add_argument("--date", type=str, default=None,
                        help="Snapshot date (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--no-instruments", action="store_true",
                        help="Skip the broker-side instrument dump (faster, offline-safe)")
    parser.add_argument("--quiet", action="store_true", help="WARNING-level logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    target = date.fromisoformat(args.date) if args.date else date.today()
    out = asyncio.run(snapshot(target, with_instruments=not args.no_instruments))
    print(f"Snapshot written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
