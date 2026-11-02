"""OpenLayers для карты на ПК — с НАШЕГО сервера, а не с зарубежного CDN.

Mini App грузит OpenLayers с cdn.jsdelivr.net. Для диспетчерской карты
(/dispatch) браузер получает файлы с того же сервера Amvera, что и страницу:
так карта не зависит от доступности jsDelivr у админа.

Откуда сервер берёт файлы: один раз скачивает пакет `ol` нужной версии из
реестра npm (registry.npmjs.org), проверяет SHA-512 архива по полю
`dist.integrity` из ответа реестра и кладёт `ol.js`/`ol.css` рядом с базой
(на Amvera — /data/vendor/ol-<версия>/). Если скачать не удалось, страница
грузит OpenLayers с jsDelivr, как Mini App (см. dispatch.js).

OpenLayers — BSD-2-Clause, проект OSGeo; файл лицензии кладётся рядом.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import tarfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

OL_VERSION = "10.5.0"  # та же версия, что в Mini App (OL_VERSION в app.js)
REGISTRY = "https://registry.npmjs.org"
MAX_TARBALL = 40 * 1024 * 1024
RETRY_AFTER = 10 * 60  # после неудачи — новая попытка не раньше чем через 10 минут
FILES = {"ol.js": "package/dist/ol.js", "ol.css": "package/ol.css"}
LICENSES = ("package/LICENSE.md", "package/LICENSE")
TYPES = {"ol.js": "application/javascript", "ol.css": "text/css"}


def check_integrity(data: bytes, integrity: str) -> bool:
    """SRI-строка реестра npm: «sha512-<base64>» (берём sha512)."""
    for part in (integrity or "").split():
        algo, _, digest = part.partition("-")
        if algo == "sha512":
            return base64.b64encode(hashlib.sha512(data).digest()).decode() == digest
    return False


def extract(tarball: bytes) -> dict[str, bytes]:
    """Достать только нужные файлы по точным именам (без распаковки архива на диск)."""
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        names = {m.name: m for m in tar.getmembers() if m.isfile()}
        for name, member in FILES.items():
            if member not in names:
                raise ValueError(f"в пакете нет {member}")
            out[name] = tar.extractfile(names[member]).read()
        for lic in LICENSES:
            if lic in names:
                out["LICENSE"] = tar.extractfile(names[lic]).read()
                break
    return out


class OlVendor:
    def __init__(self, folder: Path, version: str = OL_VERSION) -> None:
        self.folder = folder / f"ol-{version}"
        self.version = version
        self._mem: dict[str, bytes] = {}
        self._lock = asyncio.Lock()
        self._failed_at = 0.0
        self._task: asyncio.Task | None = None

    def get(self, name: str) -> bytes | None:
        if name not in FILES:
            return None
        if name not in self._mem:
            try:
                self._mem[name] = (self.folder / name).read_bytes()
            except OSError:
                return None
        return self._mem[name]

    def ready(self) -> bool:
        return all((self.folder / n).is_file() for n in FILES)

    def kick(self) -> None:
        """Начать скачивание в фоне, если файлов ещё нет (не ждём)."""
        if self.ready() or (self._task and not self._task.done()):
            return
        if time.time() - self._failed_at < RETRY_AFTER:
            return
        self._task = asyncio.create_task(self.ensure())

    async def ensure(self) -> bool:
        async with self._lock:
            if self.ready():
                return True
            try:
                files = await self._download()
                self.folder.mkdir(parents=True, exist_ok=True)
                for name, data in files.items():
                    tmp = self.folder / (name + ".tmp")
                    tmp.write_bytes(data)
                    tmp.replace(self.folder / name)
                logger.info("OpenLayers %s сохранён в %s", self.version, self.folder)
                return True
            except Exception:
                self._failed_at = time.time()
                logger.exception("Не удалось скачать OpenLayers %s из npm — карта на ПК возьмёт его с jsDelivr", self.version)
                return False

    async def _download(self) -> dict[str, bytes]:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{REGISTRY}/ol/{self.version}") as resp:
                resp.raise_for_status()
                meta = json.loads(await resp.text())
            dist = meta.get("dist") or {}
            url, integrity = dist.get("tarball") or "", dist.get("integrity") or ""
            if not url.startswith(REGISTRY + "/") or not integrity:
                raise ValueError("реестр npm вернул неожиданный ответ")
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.content.read(MAX_TARBALL + 1)
        if len(data) > MAX_TARBALL:
            raise ValueError("архив слишком большой")
        if not check_integrity(data, integrity):
            raise ValueError("контрольная сумма архива не совпала")
        return extract(data)
