from __future__ import annotations

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
    ) -> None:
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout_sec)

    async def close(self) -> None:
        await self.client.aclose()

    def _url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    async def _post_json(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self.client.post(self._url(method), json=payload)
        try:
            data = resp.json()
        except Exception as exc:
            text = resp.text[:500] if resp.text else ""
            raise TelegramApiError(
                f"{method} invalid json response ({resp.status_code}): {text}"
            ) from exc
        if resp.status_code != 200:
            raise TelegramApiError(
                f"{method} http {resp.status_code}: {data.get('description') or resp.text}"
            )
        if not data.get("ok"):
            raise TelegramApiError(
                f"{method} failed: {data.get('description') or resp.text}"
            )
        return data["result"]

    async def _post_form(
        self,
        method: str,
        data: dict[str, Any],
        files: dict[str, Any],
    ) -> dict[str, Any]:
        resp = await self.client.post(self._url(method), data=data, files=files)
        try:
            data_json = resp.json()
        except Exception as exc:
            text = resp.text[:500] if resp.text else ""
            raise TelegramApiError(
                f"{method} invalid json response ({resp.status_code}): {text}"
            ) from exc
        if resp.status_code != 200:
            raise TelegramApiError(
                f"{method} http {resp.status_code}: {data_json.get('description') or resp.text}"
            )
        if not data_json.get("ok"):
            raise TelegramApiError(
                f"{method} failed: {data_json.get('description') or resp.text}"
            )
        return data_json["result"]

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
        return await self._post_json("sendMessage", payload)

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
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
        return await self._post_json("editMessageText", payload)
