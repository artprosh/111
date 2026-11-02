"""Run the order API and web app without starting a Telegram bot.

Required server environment:
  APP_ADMIN_ID       Existing internal/admin ID used by the shared order DB
  APP_ADMIN_PHONE    Verified phone number for the first app administrator
  APP_AUTH_SECRET    At least 32 random characters
  SMSRU_API_ID       SMS.RU API key (keep it in the host's secret settings)
Optional:
  SQLITE_PATH, APP_MEDIA_DIR, APP_PUBLIC_URL, APP_HOST, APP_PORT, ORS_API_KEY
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from db import Database
from webapp.core import Api
from webapp.geodb import AddressDB
from webapp.routing import RouteService
from webapp.server import start_webapp
from webapp.standalone_auth import StandaloneAuth
from webapp.standalone_bridge import StandaloneBridge

ROOT = Path(__file__).resolve().parent
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("standalone_server")


def database_path() -> Path:
    configured = os.getenv("SQLITE_PATH")
    if configured:
        path = Path(configured)
    elif Path("/data").is_dir():
        path = Path("/data/bot.sqlite3")
    else:
        path = ROOT / "data" / "bot.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def run() -> None:
    db_path = database_path()
    db = Database(db_path)
    admin_id = int((os.getenv("APP_ADMIN_ID") or os.getenv("ADMIN_ID", "")).split(",", 1)[0].strip())
    auth = StandaloneAuth(db)
    media_dir = Path(os.getenv("APP_MEDIA_DIR", str(db_path.parent / "media")))
    bridge = StandaloneBridge(media_dir)
    api = Api(db, admin_id, bridge)
    api.public_url = (os.getenv("APP_PUBLIC_URL") or "").rstrip("/") or None
    api.geo = AddressDB(db_path.parent / "addresses.sqlite3")
    api.routing = RouteService(os.getenv("ORS_API_KEY"))
    api.background = True

    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", os.getenv("PORT", "80")))
    runner = await start_webapp(api, bridge, None, host, port, auth_service=auth)
    logger.info("Standalone Relax Gas server started on %s:%s using %s", host, port, db_path)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        db.close()


if __name__ == "__main__":
    asyncio.run(run())
