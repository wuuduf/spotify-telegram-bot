from __future__ import annotations

import configparser
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VOTIFY_CONFIG_PATH = str(PROJECT_ROOT / "config.ini")
DEFAULT_VOTIFY_WORKDIR = str(PROJECT_ROOT)
DEFAULT_COOKIES_PATH = str(PROJECT_ROOT / "cookies.txt")
DEFAULT_CACHE_FILE = str(PROJECT_ROOT / "spotify-telegram-cache.json")
DEFAULT_BOT_CONFIG_PATH = str(PROJECT_ROOT / "spotify_bot.config.toml")
DEFAULT_DOWNLOAD_ROOT = str(PROJECT_ROOT / "spotify_telegram_bot_downloads")
DEFAULT_TEMP_SUBDIR = "_tmp"


@dataclass(slots=True)
class BotConfig:
    bot_token: str
    telegram_api_base: str = "https://api.telegram.org"
    telegram_poll_timeout_sec: int = 30
    telegram_request_timeout_sec: int = 180
    telegram_send_global_interval_sec: float = 0.15
    telegram_send_chat_interval_sec: float = 0.8
    telegram_allowed_chat_ids: set[int] = field(default_factory=set)

    votify_config_path: str = DEFAULT_VOTIFY_CONFIG_PATH
    votify_workdir: str = DEFAULT_VOTIFY_WORKDIR
    python_bin: str = sys.executable

    spotify_cookies_path: str = DEFAULT_COOKIES_PATH

    download_root: str = DEFAULT_DOWNLOAD_ROOT
    temp_root: str = str(Path(DEFAULT_DOWNLOAD_ROOT) / DEFAULT_TEMP_SUBDIR)
    cache_file: str = DEFAULT_CACHE_FILE
    keep_downloads: bool = False
    max_parallel_jobs: int = 1
    max_pending_jobs: int = 50
    download_timeout_sec: int = 7200
    download_retry_count: int = 1
    download_retry_backoff_sec: float = 2.0
    drop_pending_updates_on_start: bool = True
    search_limit: int = 8

    @classmethod
    def load(cls, path: str) -> "BotConfig":
        config_path = Path(path).expanduser().resolve()
        with config_path.open("rb") as f:
            data = tomllib.load(f)

        bot_section = data.get("bot", {})
        runtime_section = data.get("runtime", {})
        votify_section = data.get("votify", {})
        spotify_section = data.get("spotify", {})

        bot_token = (
            os.environ.get("TELEGRAM_BOT_TOKEN")
            or str(bot_section.get("token", "")).strip()
        )
        if not bot_token:
            raise ValueError(
                "未找到 Telegram Bot Token。请在配置中填写 [bot].token 或设置 TELEGRAM_BOT_TOKEN。"
            )

        votify_config_path = str(
            votify_section.get("config_path", DEFAULT_VOTIFY_CONFIG_PATH)
        )
        votify_workdir = str(votify_section.get("workdir", DEFAULT_VOTIFY_WORKDIR))

        cookies_path = str(spotify_section.get("cookies_path", "")).strip()
        if not cookies_path:
            cookies_path = _read_cookies_path_from_votify_config(votify_config_path)

        allowed_ids_raw = bot_section.get("allowed_chat_ids", [])
        allowed_chat_ids: set[int] = set()
        if isinstance(allowed_ids_raw, list):
            for i in allowed_ids_raw:
                try:
                    allowed_chat_ids.add(int(i))
                except (TypeError, ValueError):
                    continue

        download_root = str(
            runtime_section.get("download_root", DEFAULT_DOWNLOAD_ROOT)
        ).strip() or DEFAULT_DOWNLOAD_ROOT
        temp_root_raw = str(runtime_section.get("temp_root", "")).strip()
        temp_root = _resolve_temp_root(
            download_root=download_root,
            temp_root_raw=temp_root_raw,
        )

        cfg = cls(
            bot_token=bot_token,
            telegram_api_base=str(
                bot_section.get("api_base", "https://api.telegram.org")
            ).rstrip("/"),
            telegram_poll_timeout_sec=max(
                1, int(bot_section.get("poll_timeout_sec", 30))
            ),
            telegram_request_timeout_sec=max(
                10, int(bot_section.get("request_timeout_sec", 180))
            ),
            telegram_send_global_interval_sec=max(
                0.0,
                float(runtime_section.get("telegram_send_global_interval_sec", 0.15)),
            ),
            telegram_send_chat_interval_sec=max(
                0.0,
                float(runtime_section.get("telegram_send_chat_interval_sec", 0.8)),
            ),
            telegram_allowed_chat_ids=allowed_chat_ids,
            votify_config_path=votify_config_path,
            votify_workdir=votify_workdir,
            python_bin=str(runtime_section.get("python_bin", sys.executable)),
            spotify_cookies_path=cookies_path,
            download_root=download_root,
            temp_root=temp_root,
            cache_file=str(
                runtime_section.get("cache_file", DEFAULT_CACHE_FILE)
            ),
            keep_downloads=bool(runtime_section.get("keep_downloads", False)),
            max_parallel_jobs=max(1, int(runtime_section.get("max_parallel_jobs", 1))),
            max_pending_jobs=max(1, int(runtime_section.get("max_pending_jobs", 50))),
            download_timeout_sec=max(
                60, int(runtime_section.get("download_timeout_sec", 7200))
            ),
            download_retry_count=max(
                0, int(runtime_section.get("download_retry_count", 1))
            ),
            download_retry_backoff_sec=max(
                0.0, float(runtime_section.get("download_retry_backoff_sec", 2.0))
            ),
            drop_pending_updates_on_start=bool(
                runtime_section.get("drop_pending_updates_on_start", True)
            ),
            search_limit=max(1, int(spotify_section.get("search_limit", 8))),
        )

        return cfg

    @property
    def has_chat_allowlist(self) -> bool:
        return len(self.telegram_allowed_chat_ids) > 0

    def ensure_dirs(self) -> None:
        Path(self.download_root).mkdir(parents=True, exist_ok=True)
        Path(self.temp_root).mkdir(parents=True, exist_ok=True)


def _read_cookies_path_from_votify_config(votify_config_path: str) -> str:
    parser = configparser.ConfigParser()
    parser.read(votify_config_path, encoding="utf-8")
    if parser.has_section("votify"):
        cookies = parser.get("votify", "cookies_path", fallback="").strip()
        if cookies:
            return cookies
    return DEFAULT_COOKIES_PATH


def _resolve_temp_root(download_root: str, temp_root_raw: str) -> str:
    """
    temp_root 统一放到 download_root 下，避免把临时大文件堆到系统 /tmp。
    """
    if not temp_root_raw or _is_system_tmp_path(temp_root_raw):
        return str(Path(download_root) / DEFAULT_TEMP_SUBDIR)
    return temp_root_raw


def _is_system_tmp_path(path_str: str) -> bool:
    p = Path(path_str).expanduser().as_posix()
    return p == "/tmp" or p.startswith("/tmp/") or p == "/var/tmp" or p.startswith("/var/tmp/")
