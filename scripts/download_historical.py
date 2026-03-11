"""Bulk download historical OHLC data from Zerodha.

Usage:
    python scripts/download_historical.py --underlying NIFTY --days 180

Downloads historical data for the specified underlying and stores in TimescaleDB.
"""

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.broker.zerodha.client import ZerodhaClient
from src.broker.zerodha.instruments import InstrumentManager
from src.config import Settings
from src.db.session import create_db_engine, create_session_factory
from src.utils.logging import setup_logging


async def main():
    parser = argparse.ArgumentParser(description="Download historical data")
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol")
    parser.add_argument("--days", type=int, default=180, help="Number of days to download")
    parser.add_argument("--interval", default="minute", help="Candle interval (minute, 5minute, etc.)")
    args = parser.parse_args()

    setup_logging("INFO")
    settings = Settings()

    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    broker = ZerodhaClient(settings.kite_api_key, settings.kite_access_token)
    await broker.connect()

    # Load instruments
    inst_manager = InstrumentManager(broker, session_factory)
    await inst_manager.load_from_db()

    # Get the index/spot instrument token
    spot_inst = inst_manager.get_by_symbol(args.underlying)
    if not spot_inst:
        # Try with "NIFTY 50" format for index
        nse_name = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FIN SERVICE"}
        spot_inst = inst_manager.get_by_symbol(nse_name.get(args.underlying, args.underlying))

    if not spot_inst:
        print(f"ERROR: Could not find instrument for {args.underlying}")
        print("Run 'python scripts/download_instruments.py' first")
        await engine.dispose()
        return

    from_date = datetime.now() - timedelta(days=args.days)
    to_date = datetime.now()

    print(f"Downloading {args.underlying} data from {from_date.date()} to {to_date.date()}")
    print(f"Instrument token: {spot_inst.instrument_token}")

    # Kite limits historical data to 60 days per request for minute data
    chunk_days = 60 if args.interval == "minute" else args.days
    current_from = from_date
    total_candles = 0

    while current_from < to_date:
        current_to = min(current_from + timedelta(days=chunk_days), to_date)
        try:
            data = await broker.get_historical_data(
                instrument_token=spot_inst.instrument_token,
                from_date=current_from,
                to_date=current_to,
                interval=args.interval,
            )
            total_candles += len(data)
            print(f"  {current_from.date()} to {current_to.date()}: {len(data)} candles")
        except Exception as e:
            print(f"  ERROR for {current_from.date()} to {current_to.date()}: {e}")

        current_from = current_to + timedelta(days=1)
        await asyncio.sleep(0.5)  # Rate limit

    print(f"\nTotal candles downloaded: {total_candles}")

    await broker.disconnect()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
