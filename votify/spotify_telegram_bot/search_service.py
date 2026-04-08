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

ASSISTED_CURATION_SEARCH_HASH = (
    "f78953bf9207d73493c27284103f5aeb6e728876d5793851bf79bc706127ff70"
)
SEARCH_SUGGESTIONS_HASH = (
    "1b44e7bced744d15c47e6c4c11952541693324020c528dc97d19c4a38cfb754e"
)
LOOKUP_ENTITY_HASH = "027903e8eb620517d49218421ddb2a4032e64c43ab0f9d015571a71ef2e31c6b"
QUERY_ARTIST_MINIMAL_HASH = (
    "53d3f76582c49ad0a05dc685955f20dc2a5f2209b192e5446e5e4e623ce23a48"
)
PATHFINDER_MAX_ITEMS = 30
HYDRATE_CONCURRENCY = 6


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
        self._uri_item_cache: dict[str, SearchItem] = {}

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
        self._uri_item_cache.clear()

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

        page_offset = max(0, offset)
        page_limit = max(1, limit)

        # song/album/artist：优先走 Pathfinder，避免命中 v1/search 的高频 429
        if normalized_kind in {"track", "album", "artist"}:
            page = await self._search_page_via_pathfinder(
                kind=kind,
                normalized_kind=normalized_kind,
                query=query,
                limit=page_limit,
                offset=page_offset,
            )
            if page is not None:
                return page
            raise RuntimeError("Search failed: Pathfinder unavailable")

        # playlist 保持现状：先 v1/search，429 时再走 Pathfinder topResults 兜底
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
                        "limit": page_limit,
                        "offset": page_offset,
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
                return self._build_page_from_rest(
                    kind=kind,
                    normalized_kind=normalized_kind,
                    query=query,
                    offset=page_offset,
                    limit=page_limit,
                    data=data,
                )

            if resp.status_code == 401 and attempt < max_attempts:
                # token 可能提前失效，主动刷新后重试
                await self.api._initialize_authorization()  # noqa: SLF001
                await asyncio.sleep(0.3)
                continue

            if resp.status_code == 429:
                fallback_page = await self._search_page_via_pathfinder(
                    kind=kind,
                    normalized_kind=normalized_kind,
                    query=query,
                    limit=page_limit,
                    offset=page_offset,
                )
                if fallback_page is not None:
                    return fallback_page
                if attempt < max_attempts:
                    retry_after = _parse_retry_after(
                        resp.headers.get("Retry-After"),
                        attempt,
                    )
                    await asyncio.sleep(retry_after)
                    continue

            detail = data or (resp.text[:500] if resp.text else "")
            raise RuntimeError(f"Search failed {resp.status_code}: {detail}")

        if resp is None:
            raise RuntimeError("Search request did not return a response")
        if resp.status_code != 200:
            detail = data or (resp.text[:500] if resp.text else "")
            raise RuntimeError(f"Search failed {resp.status_code}: {detail}")

        return self._build_page_from_rest(
            kind=kind,
            normalized_kind=normalized_kind,
            query=query,
            offset=page_offset,
            limit=page_limit,
            data=data,
        )

    def _build_page_from_rest(
        self,
        kind: str,
        normalized_kind: str,
        query: str,
        offset: int,
        limit: int,
        data: dict[str, Any],
    ) -> SearchPage:
        section_key = f"{normalized_kind}s"
        section = data.get(section_key, {})
        items = section.get("items", [])
        total = int(section.get("total", len(items)) or 0)
        parsed_items = [self._to_item(normalized_kind, item) for item in items]
        return SearchPage(
            kind=kind,
            query=query,
            offset=offset,
            limit=limit,
            total=total,
            items=parsed_items,
        )

    async def _search_page_via_pathfinder(
        self,
        kind: str,
        normalized_kind: str,
        query: str,
        limit: int,
        offset: int,
    ) -> SearchPage | None:
        if not self.api:
            return None

        request_size = min(max(limit + offset, limit), PATHFINDER_MAX_ITEMS)
        uris: list[str] = []

        try:
            assisted = await self.api._pathfinder_request(  # noqa: SLF001
                operation_name="assistedCurationSearch",
                persisted_query_hash=ASSISTED_CURATION_SEARCH_HASH,
                variables={
                    "term": query,
                    "limit": request_size,
                    "numberOfTopResults": request_size,
                },
            )
        except Exception:
            assisted = None

        if normalized_kind == "track":
            uris = self._extract_uris_from_assisted_section(assisted, "tracksV2")
        elif normalized_kind == "album":
            uris = self._extract_uris_from_assisted_section(assisted, "albumsV2")
        elif normalized_kind == "artist":
            uris = self._extract_uris_from_assisted_section(assisted, "artists")
        elif normalized_kind == "playlist":
            uris = self._extract_playlist_uris_from_top_results(assisted)
            if len(uris) < limit:
                try:
                    suggestions = await self.api._pathfinder_request(  # noqa: SLF001
                        operation_name="searchSuggestions",
                        persisted_query_hash=SEARCH_SUGGESTIONS_HASH,
                        variables={
                            "query": query,
                            "limit": request_size,
                            "numberOfTopResults": request_size,
                            "offset": 0,
                            "includeAuthors": False,
                            "includeEpisodeContentRatingsV2": False,
                        },
                    )
                    uris.extend(
                        self._extract_playlist_uris_from_top_results(suggestions)
                    )
                except Exception:
                    pass
        else:
            return None

        uris = self._dedupe_preserve_order(uris)
        total = len(uris)
        page_uris = uris[offset : offset + limit]
        if not page_uris:
            return SearchPage(
                kind=kind,
                query=query,
                offset=offset,
                limit=limit,
                total=total,
                items=[],
            )

        items = await self._hydrate_search_items(normalized_kind, page_uris)
        return SearchPage(
            kind=kind,
            query=query,
            offset=offset,
            limit=limit,
            total=total,
            items=items,
        )

    @staticmethod
    def _extract_search_v2(response: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(response, dict):
            return {}
        return response.get("data", {}).get("searchV2", {}) or {}

    def _extract_uris_from_assisted_section(
        self,
        response: dict[str, Any] | None,
        section_name: str,
    ) -> list[str]:
        search_v2 = self._extract_search_v2(response)
        section = search_v2.get(section_name) or {}
        raw_items = section.get("items") or []
        uris: list[str] = []
        for raw in raw_items:
            if section_name == "tracksV2":
                uri = (
                    ((raw or {}).get("item") or {})
                    .get("data", {})
                    .get("uri", "")
                    .strip()
                )
            else:
                uri = ((raw or {}).get("data") or {}).get("uri", "").strip()
            if uri.startswith("spotify:"):
                uris.append(uri)
        return uris

    def _extract_playlist_uris_from_top_results(
        self,
        response: dict[str, Any] | None,
    ) -> list[str]:
        search_v2 = self._extract_search_v2(response)
        top_results = (search_v2.get("topResultsV2") or {}).get("itemsV2") or []
        uris: list[str] = []
        for raw in top_results:
            uri = (
                ((raw or {}).get("item") or {})
                .get("data", {})
                .get("uri", "")
                .strip()
            )
            if uri.startswith("spotify:playlist:"):
                uris.append(uri)
        return uris

    @staticmethod
    def _dedupe_preserve_order(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    async def _hydrate_search_items(
        self,
        normalized_kind: str,
        uris: list[str],
    ) -> list[SearchItem]:
        sem = asyncio.Semaphore(HYDRATE_CONCURRENCY)

        async def _worker(uri: str) -> SearchItem | None:
            async with sem:
                return await self._hydrate_single_item(normalized_kind, uri)

        hydrated = await asyncio.gather(*(_worker(uri) for uri in uris))
        return [item for item in hydrated if item is not None]

    async def _hydrate_single_item(
        self,
        normalized_kind: str,
        uri: str,
    ) -> SearchItem | None:
        cached = self._uri_item_cache.get(uri)
        if cached is not None:
            return cached

        if not self.api:
            return None

        try:
            if normalized_kind == "artist":
                item = await self._hydrate_artist_item(uri)
            else:
                item = await self._hydrate_lookup_item(normalized_kind, uri)
        except Exception:
            return None

        if item is not None:
            self._uri_item_cache[uri] = item
        return item

    async def _hydrate_lookup_item(
        self,
        normalized_kind: str,
        uri: str,
    ) -> SearchItem | None:
        if not self.api:
            return None
        media_type, media_id = self._parse_spotify_uri(uri)
        if not media_id:
            return None

        response = await self.api._pathfinder_request(  # noqa: SLF001
            operation_name="lookupEntity",
            persisted_query_hash=LOOKUP_ENTITY_HASH,
            variables={"uri": uri},
        )
        wrappers = (response.get("data", {}).get("lookup") or [])
        if not wrappers:
            return None
        wrapper = wrappers[0] or {}
        data = wrapper.get("data") or {}
        wrapper_type = wrapper.get("__typename")

        if normalized_kind == "track":
            if wrapper_type != "TrackResponseWrapper":
                return None
            artists = ", ".join(
                (artist.get("profile") or {}).get("name", "").strip()
                for artist in ((data.get("artists") or {}).get("items") or [])
                if (artist.get("profile") or {}).get("name")
            )
            subtitle = artists or "Track"
        elif normalized_kind == "album":
            if wrapper_type != "AlbumResponseWrapper":
                return None
            artists = ", ".join(
                (artist.get("profile") or {}).get("name", "").strip()
                for artist in ((data.get("artists") or {}).get("items") or [])
                if (artist.get("profile") or {}).get("name")
            )
            subtitle = artists or "Album"
        elif normalized_kind == "playlist":
            if wrapper_type != "PlaylistResponseWrapper":
                return None
            subtitle = str(data.get("description") or "").replace("\n", " ").strip()
            if not subtitle:
                subtitle = "Playlist"
        else:
            return None

        name = str(data.get("name") or "").strip()
        if not name:
            return None
        url = self._spotify_uri_to_url(uri)
        return SearchItem(
            kind=normalized_kind,
            item_id=media_id,
            name=name,
            subtitle=subtitle,
            url=url,
        )

    async def _hydrate_artist_item(self, uri: str) -> SearchItem | None:
        if not self.api:
            return None
        media_type, media_id = self._parse_spotify_uri(uri)
        if media_type != "artist" or not media_id:
            return None

        response = await self.api._pathfinder_request(  # noqa: SLF001
            operation_name="queryArtistMinimal",
            persisted_query_hash=QUERY_ARTIST_MINIMAL_HASH,
            variables={"uri": uri},
        )
        artist_union = response.get("data", {}).get("artistUnion") or {}
        if artist_union.get("__typename") != "Artist":
            return None
        name = ((artist_union.get("profile") or {}).get("name") or "").strip()
        if not name:
            return None
        return SearchItem(
            kind="artist",
            item_id=media_id,
            name=name,
            subtitle="Artist",
            url=self._spotify_uri_to_url(uri),
        )

    @staticmethod
    def _parse_spotify_uri(uri: str) -> tuple[str, str]:
        parts = uri.split(":")
        if len(parts) >= 3 and parts[0] == "spotify":
            return parts[1], parts[2]
        return "", ""

    @staticmethod
    def _spotify_uri_to_url(uri: str) -> str:
        media_type, media_id = SpotifySearchService._parse_spotify_uri(uri)
        if media_type in {"track", "album", "artist", "playlist"} and media_id:
            return f"https://open.spotify.com/{media_type}/{media_id}"
        return ""

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
