"""Версия запущенного кода и «отпечаток» файлов фронтенда.

- commit — короткий хеш коммита из .git (Amvera кладёт .git в сборку);
  если .git нет — None.
- assets — хеш содержимого app.js и style.css. Он добавляется к их адресам
  в index.html (`/static/app.js?v=…`), поэтому после обновления кода Telegram
  на телефоне скачивает новые файлы, а не показывает сохранённую старую копию.

Только стандартная библиотека — модуль используют и aiohttp-сервер, и dev_server.py.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
ASSETS = ("app.js", "style.css")


def git_commit(root: Path = ROOT) -> str | None:
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head[:7] or None
        ref = head.split(":", 1)[1].strip()
        ref_file = git / ref
        if ref_file.is_file():
            return ref_file.read_text().strip()[:7] or None
        packed = git / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0][:7]
    except OSError:
        return None
    return None


_HASH_CACHE: dict[Path, tuple[tuple, str]] = {}


def assets_hash(static_dir: Path = STATIC_DIR) -> str:
    """Хеш app.js + style.css; пересчитывается, только когда файлы поменялись."""
    try:
        stamp = tuple((static_dir / n).stat().st_mtime_ns for n in ASSETS)
    except OSError:
        stamp = None
    hit = _HASH_CACHE.get(static_dir)
    if stamp is not None and hit and hit[0] == stamp:
        return hit[1]
    value = _assets_hash(static_dir)
    if stamp is not None:
        _HASH_CACHE[static_dir] = (stamp, value)
    return value


def _assets_hash(static_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in ASSETS:
        try:
            digest.update((static_dir / name).read_bytes())
        except OSError:
            pass
    return digest.hexdigest()[:10]


_INDEX_CACHE: dict[Path, tuple[tuple, str]] = {}


def render_index(static_dir: Path = STATIC_DIR) -> str:
    """index.html с версионированными адресами app.js/style.css и меткой версии.
    Готовая страница кешируется, пока файлы не поменялись (раньше app.js и style.css
    перечитывались и хешировались на каждое открытие приложения)."""
    try:
        stamp = tuple((static_dir / n).stat().st_mtime_ns for n in ("index.html", *ASSETS)) + (git_commit(),)
    except OSError:
        stamp = None
    hit = _INDEX_CACHE.get(static_dir)
    if stamp is not None and hit and hit[0] == stamp:
        return hit[1]
    html = _render_index(static_dir)
    if stamp is not None:
        _INDEX_CACHE[static_dir] = (stamp, html)
    return html


def _render_index(static_dir: Path) -> str:
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    v = assets_hash(static_dir)
    for name in ASSETS:
        html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={v}"')
    label = git_commit() or v
    return html.replace("</head>", f'  <meta name="app-version" content="{label}">\n</head>', 1)


def version_text() -> str:
    return f"ok\ncommit: {git_commit() or 'неизвестен'}\nassets: {assets_hash()}\n"
