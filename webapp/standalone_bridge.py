"""Non-Telegram effects for the standalone application server.

Media uploaded from the app is stored in the configured persistent directory.
Events are intentionally logged until a push provider is configured; this
bridge never calls Telegram Bot API.
"""
from __future__ import annotations

import logging
import mimetypes
import uuid
from pathlib import Path

from .crypto import RateCache

logger = logging.getLogger(__name__)


class StandaloneBridge:
    def __init__(self, media_dir: Path) -> None:
        self.media_dir = Path(media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.rates = RateCache()

    async def get_crypto_rates(self, codes: list[str]) -> dict[str, float]:
        return await self.rates.get(codes)

    async def get_crypto_rates_now(self, codes: list[str]):
        return await self.rates.get_now(codes)

    async def fetch_file(self, file_id: str):
        # Never interpret an ID as a path supplied by the request.
        name = Path(str(file_id or "")).name
        if name != file_id or not name.startswith("media-"):
            return None
        path = self.media_dir / name
        if not path.is_file():
            return None
        return path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    def _save(self, data: bytes, mime: str) -> str:
        ext = mimetypes.guess_extension(mime) or ".bin"
        if ext == ".jpe":
            ext = ".jpg"
        name = f"media-{uuid.uuid4().hex}{ext}"
        path = self.media_dir / name
        path.write_bytes(data)
        return name

    async def upload_image(self, data: bytes, mime: str, caption: str) -> str:
        return self._save(data, mime)

    async def send_payment_photo(self, order_id: int, data: bytes, mime: str, caption: str) -> str:
        return self._save(data, mime)

    async def test_log_group(self) -> bool:
        return False

    async def bot_link(self) -> str:
        return ""

    async def partner_link(self, code: str) -> str:
        return ""

    async def __getattr_async(self, name: str, *args, **kwargs):
        logger.info("Standalone event (no delivery channel configured): %s", name)
        return None

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        async def no_telegram_effect(*args, **kwargs):
            return await self.__getattr_async(name, *args, **kwargs)

        return no_telegram_effect
