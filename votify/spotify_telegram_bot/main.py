from __future__ import annotations

import argparse
import asyncio
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
    r"https://open\.spotify\.com/(track|album|playlist|artist|episode|show)/[A-Za-z0-9]{22}(?:\?[^\s]+)?"
)

HELP_TEXT = """\
Spotify Telegram Bot（M2）

命令：
/h 或 /help               显示帮助
/u <spotify-url>          下载并上传
/sg <关键词>              搜索歌曲
/sa <关键词>              搜索专辑
/sr <关键词>              搜索艺人
/sp <关键词>              搜索歌单
/s <type> <关键词>        统一搜索（type: song|album|artist|playlist）
/q                        查看队列状态

说明：
- 当前默认下载路线是 votify config.ini 中的配置（建议 web + aac-medium）。
- 你也可以直接发 Spotify 链接，等价于 /u <url>。
- 搜索结果支持按钮点选直接下载（含翻页）。
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
    kind: str
    query: str
    offset: int
    limit: int
    total: int
    items: list[SearchItem]
    created_at: float


class SpotifyTelegramBotApp:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.telegram = TelegramClient(
            token=config.bot_token,
            api_base=config.telegram_api_base,
            timeout_sec=config.telegram_request_timeout_sec,
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

        if self._is_help_command(text):
            await self.telegram.send_message(chat_id, HELP_TEXT, reply_to_message_id=message_id)
            return

        if self._is_queue_command(text):
            await self.telegram.send_message(
                chat_id,
                self._format_queue_status(),
                reply_to_message_id=message_id,
            )
            return

        if self._is_url_command(text):
            url = self._extract_url_from_command(text)
            if not url:
                await self.telegram.send_message(
                    chat_id,
                    "Usage: /u <spotify-url>",
                    reply_to_message_id=message_id,
                )
                return
            await self._enqueue_download(chat_id, url, message_id)
            return

        if self._is_search_command(text):
            await self._handle_search(chat_id, text, message_id)
            return

        if self._extract_spotify_url(text):
            url = self._extract_spotify_url(text)
            if url:
                await self._enqueue_download(chat_id, url, message_id)

    async def _handle_callback_query(self, cb: dict[str, Any]) -> None:
        cb_id = str(cb.get("id") or "")
        data = str(cb.get("data") or "").strip()
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
            await self._enqueue_download(chat_id, item.url, message_id)
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
                    text="搜索面板已关闭。你可以再次发送 /sg /sa /sr /sp /s 进行搜索。",
                    disable_web_page_preview=True,
                    reply_markup=None,
                )
            except Exception:
                pass
            return

        await self.telegram.answer_callback_query(cb_id, "Unsupported action")

    async def _handle_search(self, chat_id: int, text: str, reply_to: int | None) -> None:
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
        elif cmd in {"sp", "search_playlist"}:
            kind = "playlist"
            query = args
        else:
            parts = args.split(maxsplit=1)
            if len(parts) != 2:
                await self.telegram.send_message(
                    chat_id,
                    "Usage: /s <song|album|artist|playlist> <keywords>",
                    reply_to_message_id=reply_to,
                )
                return
            kind, query = parts[0], parts[1]

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
            kind=kind,
            query=query.strip(),
            offset=page.offset,
            limit=page.limit,
            total=page.total,
            items=page.items,
            created_at=time.time(),
        )

    async def _enqueue_download(self, chat_id: int, url: str, reply_to: int | None) -> None:
        cache_key = self._normalize_spotify_url(url)
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
            f"已加入队列（等待 {waiting}，运行 {running}）：\n{url}",
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
            for media_path in result.media_files:
                suffix = media_path.suffix.lower()
                await self.telegram.send_chat_action(chat_id, "upload_document")
                response: dict[str, Any] | None = None
                method_used: str | None = None
                try:
                    if suffix in AUDIO_EXTS:
                        response = await self.telegram.send_audio(
                            chat_id,
                            media_path,
                            reply_to_message_id=reply_to if sent == 0 else None,
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
                except TelegramApiError:
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
                await asyncio.sleep(0.2)

            if cache_entries:
                self.cache.put(cache_key, cache_entries)

            await self.telegram.send_message(
                chat_id,
                f"✅ 完成，已发送 {sent} 个文件。\n下载目录：{result.output_dir}",
                reply_to_message_id=reply_to,
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
    def _is_url_command(text: str) -> bool:
        cmd, _ = SpotifyTelegramBotApp._parse_command(text)
        return cmd in {"u", "url"}

    @staticmethod
    def _is_search_command(text: str) -> bool:
        cmd, _ = SpotifyTelegramBotApp._parse_command(text)
        return cmd in {"sg", "sa", "sr", "sp", "s", "search", "search_song", "search_album", "search_artist", "search_playlist"}

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
        return m.group(0) if m else None

    @staticmethod
    def _extract_url_from_command(text: str) -> str | None:
        _, args = SpotifyTelegramBotApp._parse_command(text)
        return SpotifyTelegramBotApp._extract_spotify_url(args)

    @staticmethod
    def _normalize_spotify_url(url: str) -> str:
        u = url.strip()
        parsed = urlparse(u)
        if parsed.scheme and parsed.netloc and parsed.path:
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return u

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
                if item.method == "audio":
                    await self.telegram.send_audio_by_file_id(
                        chat_id=chat_id,
                        file_id=item.file_id,
                        reply_to_message_id=reply_to if idx == 0 else None,
                        caption=item.file_name or None,
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
        if not file_id:
            return None
        return CachedFile(
            method=method_used,
            file_id=file_id,
            file_unique_id=file_unique_id,
            file_name=file_name,
            created_at=TelegramFileCacheStore.now_iso(),
        )


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
        "workers=%s pending_limit=%s download_timeout=%ss retry=%s backoff=%ss",
        cfg.max_parallel_jobs,
        cfg.max_pending_jobs,
        cfg.download_timeout_sec,
        cfg.download_retry_count,
        cfg.download_retry_backoff_sec,
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
