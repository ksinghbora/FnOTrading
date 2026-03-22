"""Analyze [FILTER] logs vs trade outcomes to validate filter effectiveness.

Usage:
    uv run python scripts/analyze_filters.py [logfile]
    uv run python scripts/analyze_filters.py  # reads from logs/ directory

Parses structured log lines:
  [FILTER] strategy=X filter=pcr_oi value=0.90 range=[0.7-1.5] action=PASS
  [FILTER] strategy=X filter=max_pain max_pain=23500 spot=23600 distance=0.43% ...
  [FILTER] strategy=X filter=iv_skew ... ratio=1.07 bias=NEUTRAL
  [FILTER] strategy=X filter=oi_levels ce_resistance=[...] pe_support=[...]
  [FILTER] strategy=X filter=vix value=14.2 threshold=25.0 result=pass
  [FILTER] strategy=X filter=trend ... move_pct=0.32 ... result=pass
  [ENTRY] strategy=X type=short_strangle ... total_premium=XXX
  [EXIT]  strategy=X reason=profit_target ... estimated_pnl=XXX

Produces a correlation report: filter values at entry → trade outcome.
"""

import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def parse_kv(line: str) -> dict:
    """Extract key=value pairs from a log line."""
    pairs = {}
    # Match key=value where value can be a bracketed list or a simple token
    for m in re.finditer(r'(\w+)=(\[[^\]]*\]|[^\s]+)', line):
        key, val = m.group(1), m.group(2)
        # Try numeric conversion
        try:
            val = float(val.rstrip('%'))
        except (ValueError, AttributeError):
            pass
        pairs[key] = val
    return pairs


def parse_timestamp(line: str) -> datetime | None:
    """Extract timestamp from log line (format: 2026-03-12 09:21:03,456)."""
    m = re.match(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', line)
    if m:
        return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
    return None


class TradeRecord:
    """One complete trade: filters at entry + entry details + exit outcome."""

    def __init__(self, strategy_id: str, timestamp: datetime):
        self.strategy_id = strategy_id
        self.timestamp = timestamp
        self.filters: dict[str, dict] = {}  # filter_name -> parsed kv
        self.entry: dict = {}
        self.exit: dict = {}

    @property
    def pnl(self) -> float | None:
        v = self.exit.get('estimated_pnl')
        if v is None:
            return None
        return float(v) if not isinstance(v, float) else v

    @property
    def outcome(self) -> str:
        pnl = self.pnl
        if pnl is None:
            return 'OPEN'
        return 'WIN' if pnl > 0 else 'LOSS'

    @property
    def exit_reason(self) -> str:
        return str(self.exit.get('reason', 'unknown'))


def parse_log_files(paths: list[Path]) -> list[TradeRecord]:
    """Parse log files and build trade records."""
    # Collect per-strategy filter snapshots and trades
    # Strategy -> list of filter snapshots before entry
    pending_filters: dict[str, dict[str, dict]] = defaultdict(dict)
    trades: list[TradeRecord] = []
    # Strategy -> current open trade
    open_trades: dict[str, TradeRecord] = {}

    for path in sorted(paths):
        with open(path) as f:
            for line in f:
                line = line.strip()
                ts = parse_timestamp(line)

                if '[FILTER]' in line:
                    kv = parse_kv(line)
                    sid = kv.get('strategy', '')
                    fname = kv.get('filter', '')
                    if sid and fname:
                        pending_filters[sid][fname] = kv

                elif '[ENTRY]' in line:
                    kv = parse_kv(line)
                    sid = kv.get('strategy', '')
                    if not sid:
                        continue
                    trade = TradeRecord(sid, ts or datetime.now())
                    trade.entry = kv
                    # Attach pending filters
                    trade.filters = dict(pending_filters.get(sid, {}))
                    pending_filters[sid].clear()
                    open_trades[sid] = trade

                elif '[EXIT]' in line:
                    kv = parse_kv(line)
                    sid = kv.get('strategy', '')
                    if sid and sid in open_trades:
                        open_trades[sid].exit = kv
                        trades.append(open_trades.pop(sid))

    # Include still-open trades
    for trade in open_trades.values():
        trades.append(trade)

    return trades


def print_report(trades: list[TradeRecord]) -> None:
    """Print correlation report."""
    if not trades:
        print("No trades found in logs.")
        return

    print("=" * 80)
    print("FILTER vs OUTCOME ANALYSIS")
    print("=" * 80)
    print(f"Total trades parsed: {len(trades)}")
    print()

    # ── Per-strategy summary ──
    by_strategy: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        by_strategy[t.strategy_id].append(t)

    for sid, strades in sorted(by_strategy.items()):
        closed = [t for t in strades if t.outcome != 'OPEN']
        wins = [t for t in closed if t.outcome == 'WIN']
        losses = [t for t in closed if t.outcome == 'LOSS']
        open_count = len(strades) - len(closed)

        print(f"── {sid} ──")
        print(f"  Trades: {len(closed)} closed, {open_count} open")
        if closed:
            total_pnl = sum(t.pnl for t in closed)
            avg_pnl = total_pnl / len(closed)
            print(f"  Win/Loss: {len(wins)}/{len(losses)} "
                  f"({len(wins)/len(closed)*100:.0f}% win rate)")
            print(f"  Total P&L: {total_pnl:+.2f}  |  Avg P&L: {avg_pnl:+.2f}")

            # Exit reasons
            reasons = defaultdict(int)
            for t in closed:
                reasons[t.exit_reason] += 1
            print(f"  Exit reasons: {dict(reasons)}")
        print()

    # ── Filter correlation ──
    filter_names = ['pcr_oi', 'max_pain', 'iv_skew', 'vix', 'trend']

    for fname in filter_names:
        relevant = [t for t in trades if fname in t.filters and t.outcome != 'OPEN']
        if not relevant:
            continue

        print(f"── Filter: {fname} ──")

        if fname == 'pcr_oi':
            _analyze_numeric_filter(relevant, fname, 'value')
        elif fname == 'max_pain':
            _analyze_numeric_filter(relevant, fname, 'distance')
        elif fname == 'iv_skew':
            _analyze_categorical_filter(relevant, fname, 'bias')
            _analyze_numeric_filter(relevant, fname, 'ratio')
        elif fname == 'vix':
            _analyze_numeric_filter(relevant, fname, 'value')
        elif fname == 'trend':
            _analyze_numeric_filter(relevant, fname, 'move_pct')
        print()

    # ── Detailed trade log ──
    print("── TRADE LOG (most recent first) ──")
    print(f"{'Time':<20} {'Strategy':<25} {'Outcome':<6} {'P&L':>10} "
          f"{'Reason':<15} {'PCR':>6} {'MaxPain%':>9} {'IVSkew':>7} {'VIX':>5}")
    print("-" * 115)

    for t in sorted(trades, key=lambda x: x.timestamp or datetime.min, reverse=True):
        ts_str = t.timestamp.strftime('%Y-%m-%d %H:%M') if t.timestamp else '?'
        pnl_str = f"{t.pnl:+.1f}" if t.pnl is not None else 'open'
        pcr = t.filters.get('pcr_oi', {}).get('value', '')
        mp = t.filters.get('max_pain', {}).get('distance', '')
        skew = t.filters.get('iv_skew', {}).get('ratio', '')
        vix = t.filters.get('vix', {}).get('value', '')

        pcr_s = f"{pcr:.2f}" if isinstance(pcr, float) else str(pcr)
        mp_s = f"{mp:.2f}%" if isinstance(mp, float) else str(mp)
        skew_s = f"{skew:.2f}" if isinstance(skew, float) else str(skew)
        vix_s = f"{vix:.1f}" if isinstance(vix, float) else str(vix)

        print(f"{ts_str:<20} {t.strategy_id:<25} {t.outcome:<6} {pnl_str:>10} "
              f"{t.exit_reason:<15} {pcr_s:>6} {mp_s:>9} {skew_s:>7} {vix_s:>5}")

    # ── Recommendations ──
    print()
    print("── RECOMMENDATIONS ──")
    closed = [t for t in trades if t.outcome != 'OPEN']
    if len(closed) < 5:
        print(f"  Only {len(closed)} closed trades — need 10+ for reliable correlations.")
        print("  Let the system run 3-5 trading days to accumulate data.")
    else:
        _print_recommendations(closed)


def _analyze_numeric_filter(trades: list[TradeRecord], fname: str, key: str):
    """Analyze a numeric filter value vs win/loss."""
    wins = [t for t in trades if t.outcome == 'WIN']
    losses = [t for t in trades if t.outcome == 'LOSS']

    def avg_val(group):
        vals = [t.filters[fname].get(key) for t in group
                if isinstance(t.filters[fname].get(key), (int, float))]
        return sum(vals) / len(vals) if vals else None

    w_avg = avg_val(wins)
    l_avg = avg_val(losses)

    print(f"  Avg {key} on WIN:  {w_avg:.3f}" if w_avg is not None else f"  Avg {key} on WIN:  n/a")
    print(f"  Avg {key} on LOSS: {l_avg:.3f}" if l_avg is not None else f"  Avg {key} on LOSS: n/a")

    if w_avg is not None and l_avg is not None and w_avg != l_avg:
        direction = "higher" if w_avg > l_avg else "lower"
        diff_pct = abs(w_avg - l_avg) / max(abs(w_avg), abs(l_avg)) * 100
        sig = "NOTABLE" if diff_pct > 15 else "WEAK"
        print(f"  Signal: {sig} — wins have {direction} {key} "
              f"(diff: {diff_pct:.1f}%)")


def _analyze_categorical_filter(trades: list[TradeRecord], fname: str, key: str):
    """Analyze a categorical filter value vs win/loss."""
    buckets: dict[str, dict] = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0.0})
    for t in trades:
        cat = str(t.filters[fname].get(key, 'unknown'))
        bucket = buckets[cat]
        if t.outcome == 'WIN':
            bucket['wins'] += 1
        else:
            bucket['losses'] += 1
        bucket['pnl'] += t.pnl or 0

    for cat, b in sorted(buckets.items()):
        total = b['wins'] + b['losses']
        wr = b['wins'] / total * 100 if total > 0 else 0
        print(f"  {key}={cat}: {total} trades, {wr:.0f}% win rate, P&L={b['pnl']:+.1f}")


def _print_recommendations(trades: list[TradeRecord]):
    """Print actionable recommendations based on correlations."""
    # PCR recommendation
    pcr_trades = [t for t in trades if 'pcr_oi' in t.filters
                  and isinstance(t.filters['pcr_oi'].get('value'), (int, float))]
    if len(pcr_trades) >= 5:
        wins_pcr = [t.filters['pcr_oi']['value'] for t in pcr_trades if t.outcome == 'WIN']
        losses_pcr = [t.filters['pcr_oi']['value'] for t in pcr_trades if t.outcome == 'LOSS']
        if wins_pcr and losses_pcr:
            w_avg = sum(wins_pcr) / len(wins_pcr)
            l_avg = sum(losses_pcr) / len(losses_pcr)
            if abs(w_avg - l_avg) / max(w_avg, l_avg) > 0.15:
                print(f"  PCR: Consider enabling filter — "
                      f"wins avg PCR={w_avg:.2f}, losses avg PCR={l_avg:.2f}")
            else:
                print(f"  PCR: No strong signal yet (win avg={w_avg:.2f}, loss avg={l_avg:.2f})")

    # Max pain recommendation
    mp_trades = [t for t in trades if 'max_pain' in t.filters
                 and isinstance(t.filters['max_pain'].get('distance'), (int, float))]
    if len(mp_trades) >= 5:
        wins_mp = [t.filters['max_pain']['distance'] for t in mp_trades if t.outcome == 'WIN']
        losses_mp = [t.filters['max_pain']['distance'] for t in mp_trades if t.outcome == 'LOSS']
        if wins_mp and losses_mp:
            w_avg = sum(wins_mp) / len(wins_mp)
            l_avg = sum(losses_mp) / len(losses_mp)
            if l_avg > w_avg * 1.3:
                print(f"  Max Pain: Strong signal — losses avg {l_avg:.2f}% from max pain "
                      f"vs wins {w_avg:.2f}%. Consider enabling at {(w_avg+l_avg)/2:.1f}%")
            else:
                print(f"  Max Pain: No strong signal yet "
                      f"(win dist={w_avg:.2f}%, loss dist={l_avg:.2f}%)")

    if not pcr_trades and not mp_trades:
        print("  Not enough filter data yet. Run paper trading for 3-5 days.")


def find_log_files() -> list[Path]:
    """Find log files in the project."""
    project = Path(__file__).parent.parent
    candidates = [
        project / 'logs',
        project / 'log',
        project,
    ]
    log_files = []
    for d in candidates:
        if d.is_dir():
            log_files.extend(d.glob('*.log'))
            log_files.extend(d.glob('*.log.*'))
    return log_files


def main():
    if len(sys.argv) > 1:
        paths = [Path(p) for p in sys.argv[1:]]
    else:
        paths = find_log_files()

    if not paths:
        print("No log files found. Options:")
        print("  1. Pass log file path:  uv run python scripts/analyze_filters.py trading.log")
        print("  2. Redirect live output: uv run python -m src.main 2>&1 | tee trading.log")
        print("  3. Or pipe directly:     uv run python -m src.main 2>&1 | "
              "uv run python scripts/analyze_filters.py /dev/stdin")
        return

    print(f"Parsing {len(paths)} log file(s): {[p.name for p in paths]}")
    print()

    trades = parse_log_files(paths)
    print_report(trades)


if __name__ == '__main__':
    main()
