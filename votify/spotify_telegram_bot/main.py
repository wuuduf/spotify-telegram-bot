from __future__ import annotations

import argparse
import asyncio
import io
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .cache_store import CachedFile, TelegramFileCacheStore
from .config import DEFAULT_BOT_CONFIG_PATH, BotConfig
from .downloader import DownloadResult, VotifyRunError, VotifyRunner
from .search_service import SearchItem, SearchPage, SpotifySearchService
from .telegram_client import TelegramApiError, TelegramClient

logger = logging.getLogger(__name__)

SPOTIFY_URL_RE = re.compile(
    r"https://open\.spotify\.com/(?:intl-[^/]+/)?(track|album|playlist|artist|episode|show)/[A-Za-z0-9]{22}(?:\?[^\s]+)?"
)
SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
SPOTIFY_KIND_ALIASES = {
    "song": "track",
    "track": "track",
    "album": "album",
    "artist": "artist",
    "playlist": "playlist",
    "episode": "episode",
    "show": "show",
}
WORKER_LIMIT_MIN = 1
WORKER_LIMIT_MAX = 4

HELP_TEXT = """\
Spotify Telegram Bot（M2）

命令：
/h 或 /help               显示帮助
/i                        显示 chat_id
/i <type> <id>            按类型+ID下载（type: song|track|album|artist|playlist|episode|show）
/u <spotify-url>          下载并上传（也支持 /u <type> <id>）
/rf <spotify-url>         强制刷新（清缓存并重下；也支持 <type> <id>）
/sg <关键词>              搜索歌曲
/sa <关键词>              搜索专辑
/sr <关键词>              搜索艺人
/s <type> <关键词>        统一搜索（type: song|album|artist）
/st [worker1..worker4]    查看/设置 worker 并发
/q                        查看队列状态

说明：
- 当前默认下载路线是 votify config.ini 中的配置（建议 web + aac-medium）。
- 你也可以直接发 Spotify 链接，等价于 /u <url>。
- 搜索结果支持按钮点选直接下载（含翻页、关闭，且仅发起者可操作）。
- 相同 URL 命中缓存时会直接复用 Telegram file_id 发送，不重复下载。
"""

AUDIO_EXTS = {".m4a", ".mp3", ".ogg", ".flac"}
VIDEO_EXTS = {".mp4", ".webm"}
PANEL_TTL_SECONDS = 1800  # 30 min


@dataclass(slots=True)
class SearchPanelState:
    token: str
    chat_id: int
    message_id: int
    owner_user_id: int | None
    reply_to_message_id: int | None
    kind: str
    query: str
    offset: int
    limit: int
    total: int
    items: list[SearchItem]
    created_at: float


@dataclass(slots=True)
class AudioUploadMeta:
    title: str | None = None
    performer: str | None = None
    duration_seconds: int | None = None
    thumbnail_path: Path | None = None
    thumbnail_is_temp: bool = False


class SpotifyTelegramBotApp:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.telegram = TelegramClient(
            token=config.bot_token,
            api_base=config.telegram_api_base,
            timeout_sec=config.telegram_request_timeout_sec,
            send_global_interval_sec=config.telegram_send_global_interval_sec,
            send_chat_interval_sec=config.telegram_send_chat_interval_sec,
        )
        self.runner = VotifyRunner(
            python_bin=config.python_bin,
            votify_config_path=config.votify_config_path,
            workdir=config.votify_workdir,
            download_root=config.download_root,
            temp_root=config.temp_root,
            download_timeout_sec=config.download_timeout_sec,
            download_retry_count=config.download_retry_count,
            download_retry_backoff_sec=config.download_retry_backoff_sec,
        )
        self.search = SpotifySearchService(config.spotify_cookies_path)
        self.cache = TelegramFileCacheStore(config.cache_file)
        self.offset = 0
        self.sem = asyncio.Semaphore(config.max_parallel_jobs)
        self._sem_resize_lock = asyncio.Lock()
        self._sem_shrink_tasks: set[asyncio.Task[Any]] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stat_lock = asyncio.Lock()
        self.waiting_jobs = 0
        self.running_jobs = 0
        self.finished_jobs = 0
        self._inflight_cache_keys: set[str] = set()
        self._search_panels: dict[str, SearchPanelState] = {}

    async def start(self) -> None:
        self.config.ensure_dirs()
        me = await self.telegram.get_me()
        await self.search.start()
        if self.config.drop_pending_updates_on_start:
            await self._drop_pending_updates()
        logger.info("Bot started: @%s", me.get("username", "<unknown>"))

    async def close(self) -> None:
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.search.close()
        await self.telegram.close()

    async def run_forever(self) -> None:
        try:
            await self.start()
            while True:
                try:
                    self._cleanup_expired_search_panels()
                    updates = await self.telegram.get_updates(
                        self.offset, self.config.telegram_poll_timeout_sec
                    )
                    for upd in updates:
                        self.offset = max(self.offset, upd.get("update_id", 0) + 1)
                        await self._handle_update(upd)
                except Exception as exc:
                    logger.exception("Polling error: %s", exc)
                    await asyncio.sleep(3)
        finally:
            await self.close()

    async def _drop_pending_updates(self) -> None:
        """
        启动时丢弃历史 backlog，避免重启后重复处理旧消息。
        """
        try:
            dropped = 0
            # Telegram getUpdates 单次最多返回 100 条，循环直到清空 backlog
            while True:
                updates = await self.telegram.get_updates(
                    offset=self.offset,
                    timeout_sec=0,
                    limit=100,
                )
                if not updates:
                    break
                dropped += len(updates)
                self.offset = max(
                    self.offset,
                    max(int(u.get("update_id", 0)) for u in updates) + 1,
                )
                if len(updates) < 100:
                    break
            if dropped:
                logger.info(
                    "Dropped %s pending update(s), next offset=%s",
                    dropped,
                    self.offset,
                )
        except Exception as exc:
            # 不阻塞启动
            logger.warning("Failed to drop pending updates: %s", exc)

    async def _handle_update(self, upd: dict[str, Any]) -> None:
        self._cleanup_expired_search_panels()

        cb = upd.get("callback_query")
        if cb:
            await self._handle_callback_query(cb)
            return

        msg = upd.get("message")
        if not msg:
            return
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return

        if self.config.has_chat_allowlist and chat_id not in self.config.telegram_allowed_chat_ids:
            await self.telegram.send_message(chat_id, "Not authorized for this bot.")
            return

        text = str(msg.get("text") or "").strip()
        if not text:
            return
        message_id = msg.get("message_id")
        from_user = msg.get("from") or {}
        user_id_raw = from_user.get("id")
        user_id = user_id_raw if isinstance(user_id_raw, int) else None

        if self._is_help_command(text):
            await self.telegram.send_message(chat_id, HELP_TEXT, reply_to_message_id=message_id)
            return

        if self._is_info_command(text):
            await self._handle_info_command(chat_id, text, message_id)
            return

        if self._is_settings_command(text):
            await self._handle_settings(chat_id, text, message_id)
            return

        if self._is_queue_command(text):
            await self.telegram.send_message(
                chat_id,
                self._format_queue_status(),
                reply_to_message_id=message_id,
            )
            return

        if self._is_url_command(text):
            url = self._extract_url_from_command(text, default_kind=None)
            if not url:
                await self.telegram.send_message(
                    chat_id,
                    "Usage: /u <spotify-url> 或 /u <type> <id>",
                    reply_to_message_id=message_id,
                )
                return
            await self._enqueue_download(chat_id, url, message_id)
            return

        if self._is_refresh_command(text):
            url = self._extract_url_from_command(text, default_kind=None)
            if not url:
                await self.telegram.send_message(
                    chat_id,
                    "Usage: /rf <spotify-url> 或 /rf <type> <id>",
                    reply_to_message_id=message_id,
                )
                return
            await self._enqueue_download(chat_id, url, message_id, force_refresh=True)
            return

        if self._is_search_command(text):
            await self._handle_search(chat_id, text, message_id, user_id)
            return

        if self._is_playlist_search_command(text):
            await self.telegram.send_message(
                chat_id,
                "当前版本不支持 /sp（playlist 搜索）。请使用 /sg /sa /sr /s。",
                reply_to_message_id=message_id,
            )
            return

        if self._extract_spotify_url(text):
            url = self._extract_spotify_url(text)
            if url:
                await self._enqueue_download(chat_id, url, message_id)

    async def _handle_callback_query(self, cb: dict[str, Any]) -> None:
        cb_id = str(cb.get("id") or "")
        data = str(cb.get("data") or "").strip()
        from_user = cb.get("from") or {}
        cb_user_id_raw = from_user.get("id")
        cb_user_id = cb_user_id_raw if isinstance(cb_user_id_raw, int) else None
        message = cb.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")

        if not cb_id or not isinstance(chat_id, int) or not isinstance(message_id, int):
            return

        if self.config.has_chat_allowlist and chat_id not in self.config.telegram_allowed_chat_ids:
            await self.telegram.answer_callback_query(cb_id, "Not authorized", show_alert=True)
            return

        if not data.startswith("sp:"):
            await self.telegram.answer_callback_query(cb_id, "Unsupported action")
            return

        parts = data.split(":")
        if len(parts) < 3:
            await self.telegram.answer_callback_query(cb_id, "Invalid callback data")
            return

        action = parts[1]
        token = parts[2]
        state = self._search_panels.get(token)
        if not state or state.chat_id != chat_id or state.message_id != message_id:
            await self.telegram.answer_callback_query(cb_id, "该面板已过期，请重新搜索", show_alert=True)
            return
        if state.owner_user_id is not None and cb_user_id != state.owner_user_id:
            await self.telegram.answer_callback_query(
                cb_id,
                "仅命令发起者可操作该面板",
                show_alert=True,
            )
            return

        if action == "pick":
            if len(parts) < 4:
                await self.telegram.answer_callback_query(cb_id, "无效选择")
                return
            try:
                idx = int(parts[3])
            except ValueError:
                await self.telegram.answer_callback_query(cb_id, "无效选择")
                return
            if idx < 0 or idx >= len(state.items):
                await self.telegram.answer_callback_query(cb_id, "选项不存在")
                return
            item = state.items[idx]
            if not item.url:
                await self.telegram.answer_callback_query(cb_id, "该结果没有可用链接", show_alert=True)
                return
            await self.telegram.answer_callback_query(cb_id, "已加入下载队列")
            try:
                await self.telegram.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"已选择：{item.name}\n{item.url}\n\n正在加入下载队列...",
                    disable_web_page_preview=False,
                    reply_markup=None,
                )
            except Exception:
                pass
            self._search_panels.pop(token, None)
            await self._enqueue_download(
                chat_id,
                item.url,
                state.reply_to_message_id or message_id,
            )
            return

        if action == "page":
            if len(parts) < 4:
                await self.telegram.answer_callback_query(cb_id, "无效翻页")
                return
            direction = parts[3]
            if direction not in {"prev", "next"}:
                await self.telegram.answer_callback_query(cb_id, "无效翻页")
                return

            new_offset = max(0, state.offset - state.limit) if direction == "prev" else (state.offset + state.limit)
            if direction == "next" and new_offset >= state.total:
                await self.telegram.answer_callback_query(cb_id, "已经是最后一页")
                return
            if direction == "prev" and state.offset <= 0:
                await self.telegram.answer_callback_query(cb_id, "已经是第一页")
                return

            try:
                page = await self.search.search_page(
                    kind=state.kind,
                    query=state.query,
                    limit=state.limit,
                    offset=new_offset,
                )
            except Exception as exc:
                await self.telegram.answer_callback_query(cb_id, f"翻页失败: {exc}")
                return

            state.offset = page.offset
            state.total = page.total
            state.items = page.items
            state.created_at = time.time()

            text_out = self._format_search_panel_text(page)
            markup = self._build_search_panel_markup(token, page)
            try:
                await self.telegram.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text_out,
                    disable_web_page_preview=False,
                    reply_markup=markup,
                )
            except TelegramApiError:
                # Telegram 偶发 "message is not modified"，直接忽略
                pass
            await self.telegram.answer_callback_query(cb_id)
            return

        if action == "close":
            self._search_panels.pop(token, None)
            await self.telegram.answer_callback_query(cb_id, "已关闭")
            try:
                await self.telegram.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="搜索面板已关闭。你可以再次发送 /sg /sa /sr /s 进行搜索。",
                    disable_web_page_preview=True,
                    reply_markup=None,
                )
            except Exception:
                pass
            return

        await self.telegram.answer_callback_query(cb_id, "Unsupported action")

    async def _handle_search(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None,
        owner_user_id: int | None,
    ) -> None:
        cmd, args = self._parse_command(text)
        if cmd in {"sg", "search_song"}:
            kind = "song"
            query = args
        elif cmd in {"sa", "search_album"}:
            kind = "album"
            query = args
        elif cmd in {"sr", "search_artist"}:
            kind = "artist"
            query = args
        else:
            parts = args.split(maxsplit=1)
            if len(parts) != 2:
                await self.telegram.send_message(
                    chat_id,
                    "Usage: /s <song|album|artist> <keywords>",
                    reply_to_message_id=reply_to,
                )
                return
            kind, query = parts[0], parts[1]
            kind = kind.strip().lower()
            if kind not in {"song", "album", "artist"}:
                await self.telegram.send_message(
                    chat_id,
                    "搜索类型只支持：song | album | artist",
                    reply_to_message_id=reply_to,
                )
                return

        if not query.strip():
            await self.telegram.send_message(
                chat_id,
                "请提供搜索关键词。",
                reply_to_message_id=reply_to,
            )
            return

        await self.telegram.send_chat_action(chat_id, "typing")
        try:
            page = await self.search.search_page(
                kind=kind,
                query=query.strip(),
                limit=self.config.search_limit,
                offset=0,
            )
        except Exception as exc:
            await self.telegram.send_message(
                chat_id,
                f"搜索失败: {exc}",
                reply_to_message_id=reply_to,
            )
            return

        if not page.items:
            await self.telegram.send_message(
                chat_id,
                f"没有找到结果：{kind} / {query.strip()}",
                reply_to_message_id=reply_to,
            )
            return

        token = uuid.uuid4().hex[:8]
        text_out = self._format_search_panel_text(page)
        markup = self._build_search_panel_markup(token, page)
        sent_msg = await self.telegram.send_message(
            chat_id,
            text_out,
            reply_to_message_id=reply_to,
            disable_web_page_preview=False,
            reply_markup=markup,
        )
        self._search_panels[token] = SearchPanelState(
            token=token,
            chat_id=chat_id,
            message_id=int(sent_msg.get("message_id", 0)),
            owner_user_id=owner_user_id,
            reply_to_message_id=reply_to,
            kind=kind,
            query=query.strip(),
            offset=page.offset,
            limit=page.limit,
            total=page.total,
            items=page.items,
            created_at=time.time(),
        )

    async def _enqueue_download(
        self,
        chat_id: int,
        url: str,
        reply_to: int | None,
        force_refresh: bool = False,
    ) -> None:
        cache_key = self._normalize_spotify_url(url)
        if force_refresh:
            self.cache.delete(cache_key)
        else:
            cached_sent = await self._try_send_cached(chat_id, cache_key, reply_to)
            if cached_sent:
                await self.telegram.send_message(
                    chat_id,
                    f"♻️ 命中缓存，已直接发送 {cached_sent} 个文件。",
                    reply_to_message_id=reply_to,
                )
                return

        reject_msg: str | None = None
        waiting = 0
        running = 0
        async with self._stat_lock:
            total_pending = self.waiting_jobs + self.running_jobs
            if total_pending >= self.config.max_pending_jobs:
                reject_msg = f"队列已满（上限 {self.config.max_pending_jobs}），请稍后再试。"
            elif cache_key in self._inflight_cache_keys:
                reject_msg = "该链接任务已在队列中或正在下载，请稍候。"
            else:
                self._inflight_cache_keys.add(cache_key)
                self.waiting_jobs += 1
                waiting = self.waiting_jobs
                running = self.running_jobs

        if reject_msg is not None:
            await self.telegram.send_message(
                chat_id,
                reject_msg,
                reply_to_message_id=reply_to,
            )
            return

        await self.telegram.send_message(
            chat_id,
            (
                f"{'已强制刷新并加入队列' if force_refresh else '已加入队列'}"
                f"（等待 {waiting}，运行 {running}）：\n{url}"
            ),
            reply_to_message_id=reply_to,
            disable_web_page_preview=False,
        )
        task = asyncio.create_task(
            self._process_download(chat_id, url, cache_key, reply_to)
        )
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._tasks.discard(t))

    async def _process_download(
        self,
        chat_id: int,
        url: str,
        cache_key: str,
        reply_to: int | None,
    ) -> None:
        result: DownloadResult | None = None
        run_error: VotifyRunError | None = None
        acquired = False
        started = False
        try:
            await self.sem.acquire()
            acquired = True

            async with self._stat_lock:
                self.waiting_jobs = max(0, self.waiting_jobs - 1)
                self.running_jobs += 1
                started = True

            await self.telegram.send_chat_action(chat_id, "typing")
            try:
                result = await self.runner.download_url(url)
            except VotifyRunError as exc:
                run_error = exc
                raise

            if not result.media_files:
                await self.telegram.send_message(
                    chat_id,
                    f"下载完成，但没有找到可上传媒体文件。\n目录：{result.output_dir}",
                    reply_to_message_id=reply_to,
                )
                return

            sent = 0
            cache_entries: list[CachedFile] = []
            failed_files: list[str] = []
            for media_path in result.media_files:
                suffix = media_path.suffix.lower()
                try:
                    await self.telegram.send_chat_action(chat_id, "upload_document")
                except Exception as exc:
                    # chat_action 失败不应中断整批媒体发送（常见于瞬时限流）
                    logger.debug("send_chat_action ignored: %s", exc)
                response: dict[str, Any] | None = None
                method_used: str | None = None
                audio_meta: AudioUploadMeta | None = None
                try:
                    if suffix in AUDIO_EXTS:
                        audio_meta = self._build_audio_upload_meta(
                            media_path,
                            result.temp_dir,
                        )
                        response = await self.telegram.send_audio(
                            chat_id,
                            media_path,
                            reply_to_message_id=reply_to if sent == 0 else None,
                            title=audio_meta.title,
                            performer=audio_meta.performer,
                            duration_seconds=audio_meta.duration_seconds,
                            thumbnail_path=audio_meta.thumbnail_path,
                        )
                        method_used = "audio"
                    elif suffix in VIDEO_EXTS:
                        response = await self.telegram.send_video(
                            chat_id,
                            media_path,
                            reply_to_message_id=reply_to if sent == 0 else None,
                        )
                        method_used = "video"
                    else:
                        response = await self.telegram.send_document(
                            chat_id,
                            media_path,
                            reply_to_message_id=reply_to if sent == 0 else None,
                        )
                        method_used = "document"
                    cache_entry = self._build_cache_entry(
                        method_used=method_used,
                        response=response,
                        file_name=media_path.name,
                    )
                    if cache_entry:
                        cache_entries.append(cache_entry)
                    sent += 1
                except TelegramApiError as exc:
                    logger.warning(
                        "Failed to upload as %s, fallback to document: %s (%s)",
                        "audio" if suffix in AUDIO_EXTS else "video/document",
                        media_path.name,
                        exc,
                    )
                    try:
                        response = await self.telegram.send_document(
                            chat_id,
                            media_path,
                            reply_to_message_id=reply_to if sent == 0 else None,
                        )
                        method_used = "document"
                        cache_entry = self._build_cache_entry(
                            method_used=method_used,
                            response=response,
                            file_name=media_path.name,
                        )
                        if cache_entry:
                            cache_entries.append(cache_entry)
                        sent += 1
                    except Exception as fallback_exc:
                        logger.warning(
                            "Failed to upload media after fallback: %s (%s)",
                            media_path.name,
                            fallback_exc,
                        )
                        failed_files.append(f"{media_path.name}: {fallback_exc}")
                except Exception as exc:
                    logger.warning("Failed to upload media: %s (%s)", media_path.name, exc)
                    failed_files.append(f"{media_path.name}: {exc}")
                finally:
                    if (
                        audio_meta
                        and audio_meta.thumbnail_is_temp
                        and audio_meta.thumbnail_path
                    ):
                        try:
                            audio_meta.thumbnail_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                await asyncio.sleep(0.2)

            if cache_entries:
                self.cache.put(cache_key, cache_entries)
            if failed_files and sent > 0:
                preview = "\n".join(f"- {x}" for x in failed_files[:5])
                more = ""
                if len(failed_files) > 5:
                    more = f"\n... 其余 {len(failed_files) - 5} 个失败项已省略"
                await self.telegram.send_message(
                    chat_id,
                    f"⚠️ 部分文件发送失败（成功 {sent}，失败 {len(failed_files)}）:\n{preview}{more}",
                    reply_to_message_id=reply_to,
                )
            if sent == 0 and failed_files:
                raise RuntimeError(
                    "all media upload failed: "
                    + "; ".join(failed_files[:3])
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err = str(exc)
            if len(err) > 2000:
                err = err[:2000] + "...(truncated)"
            try:
                await self.telegram.send_message(
                    chat_id,
                    f"❌ 下载失败：{err}",
                    reply_to_message_id=reply_to,
                )
            except Exception:
                pass
        finally:
            if acquired:
                self.sem.release()

            async with self._stat_lock:
                if started:
                    self.running_jobs = max(0, self.running_jobs - 1)
                    self.finished_jobs += 1
                else:
                    # 任务在等待 semaphore 时被取消，也要回收排队计数
                    self.waiting_jobs = max(0, self.waiting_jobs - 1)
                self._inflight_cache_keys.discard(cache_key)

            if result and not self.config.keep_downloads:
                self.runner.cleanup(result)
            if run_error and not self.config.keep_downloads:
                self.runner.cleanup(
                    DownloadResult(
                        job_id=run_error.job_id,
                        output_dir=run_error.output_dir,
                        temp_dir=run_error.temp_dir,
                        log_path=run_error.log_path,
                        media_files=[],
                    )
                )

    @staticmethod
    def _is_help_command(text: str) -> bool:
        cmd, _ = SpotifyTelegramBotApp._parse_command(text)
        return cmd in {"h", "help", "start"}

    @staticmethod
    def _is_info_command(text: str) -> bool:
        cmd, _ = SpotifyTelegramBotApp._parse_command(text)
        return cmd in {"i", "id"}

    @staticmethod
    def _is_settings_command(text: str) -> bool:
        cmd, _ = SpotifyTelegramBotApp._parse_command(text)
        return cmd in {"st", "settings"}

    @staticmethod
    def _is_url_command(text: str) -> bool:
        cmd, _ = SpotifyTelegramBotApp._parse_command(text)
        return cmd in {"u", "url"}

    @staticmethod
    def _is_refresh_command(text: str) -> bool:
        cmd, _ = SpotifyTelegramBotApp._parse_command(text)
        return cmd in {"rf", "refresh"}

    @staticmethod
    def _is_search_command(text: str) -> bool:
        cmd, _ = SpotifyTelegramBotApp._parse_command(text)
        return cmd in {"sg", "sa", "sr", "s", "search", "search_song", "search_album", "search_artist"}

    @staticmethod
    def _is_playlist_search_command(text: str) -> bool:
        cmd, _ = SpotifyTelegramBotApp._parse_command(text)
        return cmd in {"sp", "search_playlist"}

    @staticmethod
    def _is_queue_command(text: str) -> bool:
        cmd, _ = SpotifyTelegramBotApp._parse_command(text)
        return cmd in {"q", "queue"}

    @staticmethod
    def _parse_command(text: str) -> tuple[str, str]:
        if not text.startswith("/"):
            return "", text
        content = text[1:]
        parts = content.split(maxsplit=1)
        cmd_raw = parts[0]
        cmd = cmd_raw.split("@", 1)[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        return cmd, args

    @staticmethod
    def _extract_spotify_url(text: str) -> str | None:
        m = SPOTIFY_URL_RE.search(text)
        if not m:
            return None
        return SpotifyTelegramBotApp._normalize_spotify_url(m.group(0))

    @staticmethod
    def _normalize_kind(kind: str) -> str | None:
        return SPOTIFY_KIND_ALIASES.get(kind.strip().lower())

    @staticmethod
    def _build_spotify_url_from_kind_id(kind: str, media_id: str) -> str | None:
        normalized_kind = SpotifyTelegramBotApp._normalize_kind(kind)
        normalized_id = media_id.strip()
        if not normalized_kind or not SPOTIFY_ID_RE.fullmatch(normalized_id):
            return None
        return f"https://open.spotify.com/{normalized_kind}/{normalized_id}"

    @staticmethod
    def _extract_target_from_args(args: str, default_kind: str | None = None) -> str | None:
        raw = args.strip()
        if not raw:
            return None

        # 1) 标准 open.spotify URL
        if url := SpotifyTelegramBotApp._extract_spotify_url(raw):
            return url

        # 2) spotify URI, e.g. spotify:track:<id>
        if raw.startswith("spotify:"):
            parts = raw.split(":")
            if len(parts) >= 3:
                built = SpotifyTelegramBotApp._build_spotify_url_from_kind_id(
                    parts[1],
                    parts[2],
                )
                if built:
                    return built

        tokens = raw.split()
        # 3) /cmd <type> <id>
        if len(tokens) >= 2:
            built = SpotifyTelegramBotApp._build_spotify_url_from_kind_id(
                tokens[0],
                tokens[1],
            )
            if built:
                return built

        # 4) /cmd <id> (default kind)
        if (
            len(tokens) == 1
            and default_kind
            and SPOTIFY_ID_RE.fullmatch(tokens[0].strip())
        ):
            return SpotifyTelegramBotApp._build_spotify_url_from_kind_id(
                default_kind,
                tokens[0],
            )
        return None

    @staticmethod
    def _extract_url_from_command(text: str, default_kind: str | None = None) -> str | None:
        _, args = SpotifyTelegramBotApp._parse_command(text)
        return SpotifyTelegramBotApp._extract_target_from_args(
            args,
            default_kind=default_kind,
        )

    @staticmethod
    def _normalize_spotify_url(url: str) -> str:
        u = url.strip()
        parsed = urlparse(u)
        if parsed.scheme and parsed.netloc and parsed.path:
            path = parsed.path
            parts = [p for p in path.split("/") if p]
            # 去掉 open.spotify.com/intl-xx/ 前缀，统一缓存键
            if len(parts) >= 3 and parts[0].startswith("intl-"):
                path = "/" + "/".join(parts[1:])
            return f"{parsed.scheme}://{parsed.netloc}{path}"
        return u

    async def _handle_info_command(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None,
    ) -> None:
        _, args = self._parse_command(text)
        args = args.strip()
        if not args:
            await self.telegram.send_message(
                chat_id,
                (
                    f"chat_id: {chat_id}\n"
                    "用法：\n"
                    "- /i\n"
                    "- /i <spotify-url>\n"
                    "- /i <type> <id>  (type: song|track|album|artist|playlist|episode|show)\n"
                    "- /i <id>  (默认按 song 处理)"
                ),
                reply_to_message_id=reply_to,
            )
            return

        target = self._extract_target_from_args(args, default_kind="song")
        if not target:
            await self.telegram.send_message(
                chat_id,
                "用法：/i <spotify-url> 或 /i <type> <id> 或 /i <id>",
                reply_to_message_id=reply_to,
            )
            return
        await self._enqueue_download(chat_id, target, reply_to)

    async def _set_worker_limit(self, new_limit: int) -> tuple[int, int]:
        target = max(WORKER_LIMIT_MIN, min(int(new_limit), WORKER_LIMIT_MAX))
        async with self._sem_resize_lock:
            old = int(self.config.max_parallel_jobs)
            if old == target:
                return old, target
            self.config.max_parallel_jobs = target
            delta = target - old
            if delta > 0:
                # 扩容时，先取消旧的“缩容占位”任务并释放其占位
                for task in list(self._sem_shrink_tasks):
                    task.cancel()
                self._sem_shrink_tasks.clear()
                for _ in range(delta):
                    self.sem.release()
            else:
                hold_count = -delta

                async def _hold_permits(count: int) -> None:
                    acquired = 0
                    try:
                        for _ in range(count):
                            await self.sem.acquire()
                            acquired += 1
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        for _ in range(acquired):
                            self.sem.release()
                        raise

                task = asyncio.create_task(_hold_permits(hold_count))
                self._sem_shrink_tasks.add(task)
                self._tasks.add(task)

                def _cleanup(t: asyncio.Task[Any]) -> None:
                    self._tasks.discard(t)
                    self._sem_shrink_tasks.discard(t)

                task.add_done_callback(_cleanup)
            return old, target

    def _format_settings_status(self) -> str:
        return (
            "当前设置\n"
            f"- worker 并发: {self.config.max_parallel_jobs}\n"
            f"- 搜索每页: {self.config.search_limit}\n"
            f"- 发送节流(global/chat): "
            f"{self.config.telegram_send_global_interval_sec:.2f}s / "
            f"{self.config.telegram_send_chat_interval_sec:.2f}s"
        )

    async def _handle_settings(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None,
    ) -> None:
        _, args = self._parse_command(text)
        raw = args.strip().lower()
        if not raw:
            await self.telegram.send_message(
                chat_id,
                self._format_settings_status(),
                reply_to_message_id=reply_to,
            )
            return

        m = re.fullmatch(r"worker\s*([1-4])", raw) or re.fullmatch(
            r"worker([1-4])",
            raw,
        )
        if m:
            value = int(m.group(1))
            old, new = await self._set_worker_limit(value)
            await self.telegram.send_message(
                chat_id,
                (
                    f"已更新 worker 并发：{old} -> {new}\n"
                    "说明：降低并发时会在当前任务释放后逐步生效。"
                ),
                reply_to_message_id=reply_to,
            )
            return

        await self.telegram.send_message(
            chat_id,
            "用法：/st 或 /st worker1..worker4",
            reply_to_message_id=reply_to,
        )

    @staticmethod
    def _format_search_panel_text(page: SearchPage) -> str:
        start = page.offset + 1 if page.items else 0
        end = page.offset + len(page.items)
        lines = [
            f"搜索结果（{page.kind}）: {page.query}",
            f"第 {start}-{end} 条 / 共 {page.total} 条",
            "",
        ]
        for i, item in enumerate(page.items, start=1):
            subtitle = f" — {item.subtitle}" if item.subtitle else ""
            lines.append(f"{i}. {item.name}{subtitle}")
        lines.append("")
        lines.append("点击下方按钮可直接下载。")
        return "\n".join(lines)

    @staticmethod
    def _build_search_panel_markup(token: str, page: SearchPage) -> dict[str, Any]:
        keyboard: list[list[dict[str, str]]] = []

        for idx, item in enumerate(page.items):
            name = item.name.strip() or "(No title)"
            text = f"{idx + 1}. {name}"
            if len(text) > 60:
                text = text[:57] + "..."
            keyboard.append(
                [
                    {
                        "text": text,
                        "callback_data": f"sp:pick:{token}:{idx}",
                    }
                ]
            )

        nav_row: list[dict[str, str]] = []
        if page.has_prev:
            nav_row.append({"text": "⬅️ 上一页", "callback_data": f"sp:page:{token}:prev"})
        if page.has_next:
            nav_row.append({"text": "下一页 ➡️", "callback_data": f"sp:page:{token}:next"})
        if nav_row:
            keyboard.append(nav_row)

        keyboard.append([{"text": "❌ 关闭", "callback_data": f"sp:close:{token}"}])
        return {"inline_keyboard": keyboard}

    def _cleanup_expired_search_panels(self) -> None:
        now = time.time()
        expired_tokens = [
            token
            for token, state in self._search_panels.items()
            if now - state.created_at > PANEL_TTL_SECONDS
        ]
        for token in expired_tokens:
            self._search_panels.pop(token, None)

    def _format_queue_status(self) -> str:
        return (
            "队列状态\n"
            f"- 等待中: {self.waiting_jobs}\n"
            f"- 运行中: {self.running_jobs}\n"
            f"- 已完成: {self.finished_jobs}\n"
            f"- 队列上限: {self.config.max_pending_jobs}\n"
            f"- 搜索面板: {len(self._search_panels)}\n"
            f"- Worker 并发: {self.config.max_parallel_jobs}"
        )

    async def _try_send_cached(
        self,
        chat_id: int,
        cache_key: str,
        reply_to: int | None,
    ) -> int:
        items = self.cache.get(cache_key)
        if not items:
            return 0

        sent = 0
        for idx, item in enumerate(items):
            try:
                if (
                    item.method == "document"
                    and Path(item.file_name or "").suffix.lower() in AUDIO_EXTS
                ):
                    # 历史缓存可能把音频错误固化为 document，这里主动回源重传一次
                    self.cache.delete(cache_key)
                    return 0
                if item.method == "audio":
                    await self.telegram.send_audio_by_file_id(
                        chat_id=chat_id,
                        file_id=item.file_id,
                        reply_to_message_id=reply_to if idx == 0 else None,
                        caption=item.file_name or None,
                        title=item.title or None,
                        performer=item.performer or None,
                        duration_seconds=item.duration if item.duration > 0 else None,
                    )
                elif item.method == "video":
                    await self.telegram.send_video_by_file_id(
                        chat_id=chat_id,
                        file_id=item.file_id,
                        reply_to_message_id=reply_to if idx == 0 else None,
                        caption=item.file_name or None,
                    )
                else:
                    await self.telegram.send_document_by_file_id(
                        chat_id=chat_id,
                        file_id=item.file_id,
                        reply_to_message_id=reply_to if idx == 0 else None,
                        caption=item.file_name or None,
                    )
                sent += 1
            except Exception:
                # 缓存失效，删除并回源下载
                self.cache.delete(cache_key)
                return 0
        return sent

    @staticmethod
    def _build_cache_entry(
        method_used: str | None,
        response: dict[str, Any] | None,
        file_name: str,
    ) -> CachedFile | None:
        if not method_used or not response:
            return None
        if method_used == "audio":
            node = response.get("audio") or {}
        elif method_used == "video":
            node = response.get("video") or {}
        else:
            node = response.get("document") or {}
        file_id = str(node.get("file_id") or "").strip()
        file_unique_id = str(node.get("file_unique_id") or "").strip()
        title = ""
        performer = ""
        duration = 0
        if method_used == "audio":
            title = str(node.get("title") or "").strip()
            performer = str(node.get("performer") or "").strip()
            try:
                duration = int(node.get("duration") or 0)
            except Exception:
                duration = 0
        if not file_id:
            return None
        return CachedFile(
            method=method_used,
            file_id=file_id,
            file_unique_id=file_unique_id,
            file_name=file_name,
            title=title,
            performer=performer,
            duration=duration,
            created_at=TelegramFileCacheStore.now_iso(),
        )

    @staticmethod
    def _build_audio_upload_meta(media_path: Path, temp_dir: Path) -> AudioUploadMeta:
        meta = AudioUploadMeta()
        try:
            from mutagen import File as MutagenFile  # type: ignore[import-not-found]
        except Exception:
            return meta

        audio_obj: Any | None = None
        try:
            audio_obj = MutagenFile(str(media_path))
        except Exception:
            audio_obj = None
        if not audio_obj:
            return meta

        tags = getattr(audio_obj, "tags", None)
        if tags:
            meta.title = SpotifyTelegramBotApp._pick_tag_text(
                tags,
                ("title", "\xa9nam", "TIT2"),
            )
            meta.performer = SpotifyTelegramBotApp._pick_tag_text(
                tags,
                ("artist", "\xa9ART", "TPE1", "albumartist", "aART"),
            )

        info = getattr(audio_obj, "info", None)
        try:
            length = float(getattr(info, "length", 0) or 0)
            if length > 0:
                meta.duration_seconds = max(1, int(round(length)))
        except Exception:
            meta.duration_seconds = None

        cover_bytes = SpotifyTelegramBotApp._extract_cover_bytes(audio_obj)
        thumb_path = SpotifyTelegramBotApp._create_thumbnail_from_cover(
            cover_bytes=cover_bytes,
            fallback_image_path=SpotifyTelegramBotApp._find_sidecar_cover(media_path),
            temp_dir=temp_dir,
        )
        if thumb_path:
            meta.thumbnail_path = thumb_path
            meta.thumbnail_is_temp = True
        return meta

    @staticmethod
    def _pick_tag_text(tags: Any, keys: tuple[str, ...]) -> str | None:
        for key in keys:
            try:
                value = tags.get(key)
            except Exception:
                value = None
            text = SpotifyTelegramBotApp._tag_value_to_text(value)
            if text:
                return text
        return None

    @staticmethod
    def _tag_value_to_text(value: Any) -> str | None:
        if value is None:
            return None
        if hasattr(value, "text"):
            value = getattr(value, "text")
        if isinstance(value, (list, tuple, set)):
            for item in value:
                text = SpotifyTelegramBotApp._tag_value_to_text(item)
                if text:
                    return text
            return None
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8", errors="ignore")
            except Exception:
                return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _extract_cover_bytes(audio_obj: Any) -> bytes | None:
        tags = getattr(audio_obj, "tags", None)
        if tags:
            try:
                covr = tags.get("covr")
                if covr:
                    first = covr[0]
                    data = bytes(first)
                    if data:
                        return data
            except Exception:
                pass
            try:
                for key in tags.keys():
                    if str(key).startswith("APIC"):
                        frame = tags.get(key)
                        data = getattr(frame, "data", None)
                        if data:
                            return bytes(data)
            except Exception:
                pass

        pictures = getattr(audio_obj, "pictures", None)
        if pictures:
            try:
                first_pic = pictures[0]
                data = getattr(first_pic, "data", None)
                if data:
                    return bytes(data)
            except Exception:
                pass
        return None

    @staticmethod
    def _find_sidecar_cover(media_path: Path) -> Path | None:
        candidates = [
            media_path.with_suffix(".jpg"),
            media_path.with_suffix(".jpeg"),
            media_path.with_suffix(".png"),
            media_path.with_suffix(".webp"),
            media_path.parent / "cover.jpg",
            media_path.parent / "cover.jpeg",
            media_path.parent / "cover.png",
            media_path.parent / "folder.jpg",
            media_path.parent / "folder.jpeg",
            media_path.parent / "folder.png",
        ]
        for path in candidates:
            if path.exists() and path.is_file():
                return path
        return None

    @staticmethod
    def _create_thumbnail_from_cover(
        cover_bytes: bytes | None,
        fallback_image_path: Path | None,
        temp_dir: Path,
    ) -> Path | None:
        try:
            from PIL import Image
        except Exception:
            return None

        image: Any | None = None
        try:
            if cover_bytes:
                image = Image.open(io.BytesIO(cover_bytes))
            elif fallback_image_path:
                image = Image.open(fallback_image_path)
            else:
                return None
            image = image.convert("RGB")
            try:
                resample = Image.Resampling.LANCZOS  # Pillow >= 9
            except Exception:
                resample = Image.LANCZOS
            image.thumbnail((320, 320), resample=resample)
            temp_dir.mkdir(parents=True, exist_ok=True)
            thumb_path = temp_dir / f"tgthumb-{uuid.uuid4().hex}.jpg"
            image.save(thumb_path, format="JPEG", quality=88, optimize=True)
            if thumb_path.stat().st_size > 200 * 1024:
                for q in (80, 72, 64, 56):
                    image.save(thumb_path, format="JPEG", quality=q, optimize=True)
                    if thumb_path.stat().st_size <= 200 * 1024:
                        break
            return thumb_path
        except Exception:
            return None
        finally:
            if image is not None:
                try:
                    image.close()
                except Exception:
                    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Spotify Telegram Bot (M2)")
    parser.add_argument(
        "--config",
        default=DEFAULT_BOT_CONFIG_PATH,
        help="Bot config TOML path",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level, e.g. DEBUG/INFO/WARNING",
    )
    return parser.parse_args()


async def _async_main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="[%(asctime)s] [%(levelname)s] %(message)s",
    )

    cfg = BotConfig.load(args.config)
    logger.info("Loaded config: %s", Path(args.config).resolve())
    logger.info("votify config: %s", cfg.votify_config_path)
    logger.info("download root: %s", cfg.download_root)
    logger.info("temp root: %s", cfg.temp_root)
    logger.info("cache file: %s", cfg.cache_file)
    logger.info(
        "workers=%s pending_limit=%s download_timeout=%ss retry=%s backoff=%ss send_limit(global/chat)=%.2fs/%.2fs",
        cfg.max_parallel_jobs,
        cfg.max_pending_jobs,
        cfg.download_timeout_sec,
        cfg.download_retry_count,
        cfg.download_retry_backoff_sec,
        cfg.telegram_send_global_interval_sec,
        cfg.telegram_send_chat_interval_sec,
    )

    while True:
        app = SpotifyTelegramBotApp(cfg)
        try:
            await app.run_forever()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Bot crashed, restarting in 5 seconds: %s", exc)
            try:
                await app.close()
            except Exception:
                pass
            await asyncio.sleep(5)


def main() -> None:
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
