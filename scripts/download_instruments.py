"""Download and store instrument master data from Zerodha.

Usage:
    python scripts/download_instruments.py

Run this daily before market opens (e.g., 8:30 AM IST).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.broker.zerodha.client import ZerodhaClient
from src.broker.zerodha.instruments import InstrumentManager
from src.config import Settings
from src.db.session import create_db_engine, create_session_factory
from src.utils.logging import setup_logging


async def main():
    setup_logging("INFO")
    settings = Settings()

    # Create database engine
    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    # Create broker client
    broker = ZerodhaClient(settings.kite_api_key, settings.kite_access_token)
    await broker.connect()

    # Download and store instruments
    manager = InstrumentManager(broker, session_factory)
    count = await manager.download_and_store()

    print(f"\nDownloaded and stored {count} instruments")

    # Show summary
    fno_count = len(manager.get_fno_instruments())
    nifty_opts = len(manager.get_fno_instruments(underlying="NIFTY"))
    bnifty_opts = len(manager.get_fno_instruments(underlying="BANKNIFTY"))

    print(f"  F&O instruments: {fno_count}")
    print(f"  NIFTY options/futures: {nifty_opts}")
    print(f"  BANKNIFTY options/futures: {bnifty_opts}")

    await broker.disconnect()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
