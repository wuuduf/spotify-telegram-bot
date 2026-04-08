from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

import httpx


class TelegramApiError(RuntimeError):
    pass


class TelegramClient:
    def __init__(
        self,
        token: str,
        api_base: str = "https://api.telegram.org",
        timeout_sec: int = 180,
        send_global_interval_sec: float = 0.15,
        send_chat_interval_sec: float = 0.8,
        max_retry_attempts: int = 3,
    ) -> None:
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout_sec)
        self.send_global_interval_sec = max(0.0, float(send_global_interval_sec))
        self.send_chat_interval_sec = max(0.0, float(send_chat_interval_sec))
        self.max_retry_attempts = max(1, int(max_retry_attempts))
        self._send_lock = asyncio.Lock()
        self._last_global_send_at = 0.0
        self._last_chat_send_at: dict[int, float] = {}

    async def close(self) -> None:
        await self.client.aclose()

    def _url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    async def _wait_send_slot(self, chat_id: int | None) -> None:
        if self.send_global_interval_sec <= 0 and self.send_chat_interval_sec <= 0:
            return
        async with self._send_lock:
            while True:
                now = time.monotonic()
                wait_sec = 0.0
                if self.send_global_interval_sec > 0:
                    wait_sec = max(
                        wait_sec,
                        self.send_global_interval_sec - (now - self._last_global_send_at),
                    )
                if chat_id is not None and self.send_chat_interval_sec > 0:
                    last_chat = self._last_chat_send_at.get(int(chat_id), 0.0)
                    wait_sec = max(
                        wait_sec,
                        self.send_chat_interval_sec - (now - last_chat),
                    )
                if wait_sec <= 0:
                    stamp = time.monotonic()
                    if self.send_global_interval_sec > 0:
                        self._last_global_send_at = stamp
                    if chat_id is not None and self.send_chat_interval_sec > 0:
                        self._last_chat_send_at[int(chat_id)] = stamp
                    if len(self._last_chat_send_at) > 4096:
                        # 控制内存占用，粗略清理最老一半记录
                        oldest = sorted(
                            self._last_chat_send_at.items(),
                            key=lambda kv: kv[1],
                        )[:2048]
                        for chat, _ in oldest:
                            self._last_chat_send_at.pop(chat, None)
                    return
                await asyncio.sleep(wait_sec)

    @staticmethod
    def _extract_retry_after_seconds(data: dict[str, Any], fallback_text: str) -> float | None:
        try:
            params = data.get("parameters") or {}
            retry_after = params.get("retry_after")
            if retry_after is not None:
                sec = float(retry_after)
                if sec > 0:
                    return min(sec, 60.0)
        except Exception:
            pass

        text = str(fallback_text or "")
        m = re.search(r"retry after\s+(\d+)", text, flags=re.IGNORECASE)
        if m:
            try:
                sec = float(m.group(1))
                if sec > 0:
                    return min(sec, 60.0)
            except Exception:
                pass
        return None

    async def _post_json(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: TelegramApiError | None = None
        for attempt in range(1, self.max_retry_attempts + 1):
            try:
                resp = await self.client.post(self._url(method), json=payload)
            except httpx.HTTPError as exc:
                if attempt >= self.max_retry_attempts:
                    raise TelegramApiError(f"{method} request failed: {exc}") from exc
                await asyncio.sleep(min(1.5 * attempt, 6.0))
                continue

            try:
                data = resp.json()
            except Exception as exc:
                text = resp.text[:500] if resp.text else ""
                raise TelegramApiError(
                    f"{method} invalid json response ({resp.status_code}): {text}"
                ) from exc

            if resp.status_code == 200 and data.get("ok"):
                return data["result"]

            detail = str(data.get("description") or resp.text or "").strip()
            if resp.status_code == 429 and attempt < self.max_retry_attempts:
                retry_after = self._extract_retry_after_seconds(data, detail)
                await asyncio.sleep(retry_after if retry_after is not None else min(1.8 * attempt, 8.0))
                continue

            if resp.status_code != 200:
                last_error = TelegramApiError(f"{method} http {resp.status_code}: {detail}")
            else:
                last_error = TelegramApiError(f"{method} failed: {detail}")
            break

        if last_error:
            raise last_error
        raise TelegramApiError(f"{method} failed: unknown error")

    async def _post_form(
        self,
        method: str,
        data: dict[str, Any],
        files: dict[str, Any],
    ) -> dict[str, Any]:
        last_error: TelegramApiError | None = None
        for attempt in range(1, self.max_retry_attempts + 1):
            # 发生重试时，上传文件句柄需要回到开头，否则会发送空内容
            for file_item in files.values():
                fp: Any | None = None
                if isinstance(file_item, tuple) and len(file_item) >= 2:
                    fp = file_item[1]
                elif hasattr(file_item, "read"):
                    fp = file_item
                if fp is not None and hasattr(fp, "seek"):
                    try:
                        fp.seek(0)
                    except Exception:
                        pass

            try:
                resp = await self.client.post(self._url(method), data=data, files=files)
            except httpx.HTTPError as exc:
                if attempt >= self.max_retry_attempts:
                    raise TelegramApiError(f"{method} request failed: {exc}") from exc
                await asyncio.sleep(min(1.5 * attempt, 6.0))
                continue

            try:
                data_json = resp.json()
            except Exception as exc:
                text = resp.text[:500] if resp.text else ""
                raise TelegramApiError(
                    f"{method} invalid json response ({resp.status_code}): {text}"
                ) from exc

            if resp.status_code == 200 and data_json.get("ok"):
                return data_json["result"]

            detail = str(data_json.get("description") or resp.text or "").strip()
            if resp.status_code == 429 and attempt < self.max_retry_attempts:
                retry_after = self._extract_retry_after_seconds(data_json, detail)
                await asyncio.sleep(retry_after if retry_after is not None else min(1.8 * attempt, 8.0))
                continue

            if resp.status_code != 200:
                last_error = TelegramApiError(f"{method} http {resp.status_code}: {detail}")
            else:
                last_error = TelegramApiError(f"{method} failed: {detail}")
            break

        if last_error:
            raise last_error
        raise TelegramApiError(f"{method} failed: unknown error")

    async def get_me(self) -> dict[str, Any]:
        return await self._post_json("getMe", {})

    async def get_updates(
        self,
        offset: int,
        timeout_sec: int = 30,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._post_json(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout_sec,
                "limit": max(1, min(int(limit), 100)),
                "allowed_updates": ["message", "callback_query"],
            },
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        disable_web_page_preview: bool = True,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self._wait_send_slot(chat_id)
        return await self._post_json("sendMessage", payload)

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self._wait_send_slot(chat_id)
        await self._post_json(
            "sendChatAction",
            {
                "chat_id": chat_id,
                "action": action,
            },
        )

    async def send_audio(
        self,
        chat_id: int,
        audio_path: Path,
        reply_to_message_id: int | None = None,
        caption: str | None = None,
        title: str | None = None,
        performer: str | None = None,
        duration_seconds: int | None = None,
        thumbnail_path: Path | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if caption:
            payload["caption"] = caption
        if title:
            payload["title"] = title
        if performer:
            payload["performer"] = performer
        if duration_seconds and duration_seconds > 0:
            payload["duration"] = int(duration_seconds)

        await self._wait_send_slot(chat_id)
        audio_fp = audio_path.open("rb")
        thumb_fp = None
        try:
            files: dict[str, Any] = {"audio": (audio_path.name, audio_fp)}
            if thumbnail_path and thumbnail_path.exists():
                thumb_fp = thumbnail_path.open("rb")
                files["thumbnail"] = (thumbnail_path.name, thumb_fp, "image/jpeg")
            return await self._post_form(
                "sendAudio",
                data=payload,
                files=files,
            )
        finally:
            audio_fp.close()
            if thumb_fp:
                thumb_fp.close()

    async def send_audio_by_file_id(
        self,
        chat_id: int,
        file_id: str,
        reply_to_message_id: int | None = None,
        caption: str | None = None,
        title: str | None = None,
        performer: str | None = None,
        duration_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "audio": file_id,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if caption:
            payload["caption"] = caption
        if title:
            payload["title"] = title
        if performer:
            payload["performer"] = performer
        if duration_seconds and duration_seconds > 0:
            payload["duration"] = int(duration_seconds)
        await self._wait_send_slot(chat_id)
        return await self._post_json("sendAudio", payload)

    async def send_document(
        self,
        chat_id: int,
        doc_path: Path,
        reply_to_message_id: int | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if caption:
            payload["caption"] = caption

        await self._wait_send_slot(chat_id)
        with doc_path.open("rb") as fp:
            return await self._post_form(
                "sendDocument",
                data=payload,
                files={"document": (doc_path.name, fp)},
            )

    async def send_document_by_file_id(
        self,
        chat_id: int,
        file_id: str,
        reply_to_message_id: int | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "document": file_id,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if caption:
            payload["caption"] = caption
        await self._wait_send_slot(chat_id)
        return await self._post_json("sendDocument", payload)

    async def send_video(
        self,
        chat_id: int,
        video_path: Path,
        reply_to_message_id: int | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if caption:
            payload["caption"] = caption

        await self._wait_send_slot(chat_id)
        with video_path.open("rb") as fp:
            return await self._post_form(
                "sendVideo",
                data=payload,
                files={"video": (video_path.name, fp)},
            )

    async def send_video_by_file_id(
        self,
        chat_id: int,
        file_id: str,
        reply_to_message_id: int | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "video": file_id,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if caption:
            payload["caption"] = caption
        await self._wait_send_slot(chat_id)
        return await self._post_json("sendVideo", payload)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text
        return await self._post_json("answerCallbackQuery", payload)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        disable_web_page_preview: bool = True,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._wait_send_slot(chat_id)
        return await self._post_json("editMessageText", payload)
