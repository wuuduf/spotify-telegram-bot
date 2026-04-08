from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx

from votify.api.api import SpotifyApi
from votify.api.enums import SessionType


TYPE_MAP = {
    "song": "track",
    "track": "track",
    "album": "album",
    "artist": "artist",
    "playlist": "playlist",
}


@dataclass(slots=True)
class SearchItem:
    kind: str
    item_id: str
    name: str
    subtitle: str
    url: str


@dataclass(slots=True)
class SearchPage:
    kind: str
    query: str
    offset: int
    limit: int
    total: int
    items: list[SearchItem]

    @property
    def has_prev(self) -> bool:
        return self.offset > 0

    @property
    def has_next(self) -> bool:
        return self.offset + len(self.items) < self.total


class SpotifySearchService:
    def __init__(self, cookies_path: str) -> None:
        self.cookies_path = cookies_path
        self.api: SpotifyApi | None = None
        self.search_client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self.api = await SpotifyApi.create_from_netscape_cookies(
            self.cookies_path,
            session_type=SessionType.WEB,
        )
        proxy_url = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
        )
        self.search_client = httpx.AsyncClient(timeout=20, proxy=proxy_url)

    async def close(self) -> None:
        if self.search_client:
            await self.search_client.aclose()
            self.search_client = None
        if self.api:
            await self.api.client.aclose()
            self.api = None

    async def search(
        self,
        kind: str,
        query: str,
        limit: int = 8,
    ) -> list[SearchItem]:
        page = await self.search_page(kind=kind, query=query, limit=limit, offset=0)
        return page.items

    async def search_page(
        self,
        kind: str,
        query: str,
        limit: int = 8,
        offset: int = 0,
    ) -> SearchPage:
        if not self.api or not self.search_client:
            raise RuntimeError("Search service not initialized")

        normalized_kind = TYPE_MAP.get(kind.lower())
        if not normalized_kind:
            raise ValueError(f"Unsupported search kind: {kind}")

        resp: httpx.Response | None = None
        data: dict[str, Any] = {}
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            await self.api._refresh_authorization_if_needed()  # noqa: SLF001
            headers = {
                "authorization": self.api.client.headers.get("authorization", ""),
                "client-token": self.api.client.headers.get("client-token", ""),
                "accept": "application/json",
                "app-platform": "WebPlayer",
                "origin": "https://open.spotify.com",
                "referer": "https://open.spotify.com/",
                "user-agent": self.api.client.headers.get("user-agent", "Mozilla/5.0"),
            }

            try:
                resp = await self.search_client.get(
                    "https://api.spotify.com/v1/search",
                    params={
                        "q": query,
                        "type": normalized_kind,
                        "limit": limit,
                        "offset": max(0, offset),
                    },
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                if attempt >= max_attempts:
                    raise RuntimeError(f"Search request failed: {exc}") from exc
                await asyncio.sleep(attempt)
                continue

            try:
                data = resp.json()
            except Exception:
                data = {}

            if resp.status_code == 200:
                break

            if resp.status_code == 401 and attempt < max_attempts:
                # token 可能提前失效，主动刷新后重试
                await self.api._initialize_authorization()  # noqa: SLF001
                await asyncio.sleep(0.3)
                continue

            if resp.status_code == 429 and attempt < max_attempts:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"), attempt)
                await asyncio.sleep(retry_after)
                continue

            detail = data or (resp.text[:500] if resp.text else "")
            raise RuntimeError(f"Search failed {resp.status_code}: {detail}")

        if resp is None:
            raise RuntimeError("Search request did not return a response")
        if resp.status_code != 200:
            detail = data or (resp.text[:500] if resp.text else "")
            raise RuntimeError(f"Search failed {resp.status_code}: {detail}")

        section_key = f"{normalized_kind}s"
        section = data.get(section_key, {})
        items = section.get("items", [])
        total = int(section.get("total", len(items)) or 0)
        parsed_items = [self._to_item(normalized_kind, item) for item in items]
        return SearchPage(
            kind=kind,
            query=query,
            offset=max(0, offset),
            limit=limit,
            total=total,
            items=parsed_items,
        )

    @staticmethod
    def _to_item(kind: str, item: dict[str, Any]) -> SearchItem:
        if kind == "track":
            artists = ", ".join(a.get("name", "") for a in item.get("artists", []))
            subtitle = f"{artists} | {item.get('album', {}).get('name', '')}".strip(" |")
            url = item.get("external_urls", {}).get("spotify", "")
        elif kind == "album":
            artists = ", ".join(a.get("name", "") for a in item.get("artists", []))
            subtitle = artists
            url = item.get("external_urls", {}).get("spotify", "")
        elif kind == "artist":
            followers = item.get("followers", {}).get("total")
            subtitle = f"Followers: {followers}" if followers else "Artist"
            url = item.get("external_urls", {}).get("spotify", "")
        else:
            owner = item.get("owner", {}).get("display_name", "")
            subtitle = owner or "Playlist"
            url = item.get("external_urls", {}).get("spotify", "")

        return SearchItem(
            kind=kind,
            item_id=item.get("id", ""),
            name=item.get("name", ""),
            subtitle=subtitle,
            url=url,
        )


def _parse_retry_after(value: str | None, attempt: int) -> float:
    if value:
        try:
            sec = float(value)
            if sec > 0:
                return min(sec, 30.0)
        except (TypeError, ValueError):
            pass
    return min(2.0 * attempt, 10.0)
