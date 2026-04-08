import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Collection

import httpx

from ..utils import safe_json
from .constants import TOTP_DIGITS, TOTP_PERIOD, TOTP_SECRETS_URL
from .exceptions import VotifyRequestException

logger = logging.getLogger(__name__)


class Totp:
    def __init__(
        self,
        version: str,
        secret: bytes,
    ) -> None:
        self.version = version
        self.secret = secret

    @classmethod
    async def initialize(cls) -> "Totp":
        # Try local cache file first (useful when git.gay is unreachable)
        local_cache = Path(__file__).parent.parent.parent / "totp_secrets.json"
        secrets = None

        try:
            import os
            _proxy = (
                os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
            )
            async with httpx.AsyncClient(proxy=_proxy) as client:
                response = await client.get(TOTP_SECRETS_URL, timeout=10)
            secrets = safe_json(response)
            if response.status_code != 200 or not secrets:
                secrets = None
            else:
                logger.debug(f"Received TOTP secrets from network: {secrets}")
                # Save to local cache for future offline use
                try:
                    local_cache.write_text(json.dumps(secrets))
                    logger.debug(f"Cached TOTP secrets to {local_cache}")
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Failed to fetch TOTP secrets from network: {e}")

        if secrets is None:
            if local_cache.exists():
                logger.warning(f"Using cached TOTP secrets from {local_cache}")
                secrets = json.loads(local_cache.read_text())
            else:
                raise VotifyRequestException(
                    name="TOTP secrets",
                    response_status_code=0,
                    response_text=(
                        f"Network request failed and no local cache found at {local_cache}. "
                        f"Please manually download {TOTP_SECRETS_URL} and save it as totp_secrets.json "
                        f"in the votify root directory."
                    ),
                )

        version = max(secrets.keys(), key=int)

        return cls(
            version=version,
            secret=cls.derive(secrets[version]),
        )

    @staticmethod
    def derive(ciphertext: Collection[int]) -> bytes:
        return "".join(
            str(byte ^ ((i % 33) + 9)) for i, byte in enumerate(ciphertext)
        ).encode("ascii")

    def generate(self, timestamp: int) -> str:
        counter = int(timestamp) // 1000 // TOTP_PERIOD
        counter_bytes = counter.to_bytes(8, "big")

        h = hmac.new(self.secret, counter_bytes, hashlib.sha1)
        hmac_result = h.digest()

        offset = hmac_result[-1] & 0x0F
        binary = (
            (hmac_result[offset] & 0x7F) << 24
            | (hmac_result[offset + 1] & 0xFF) << 16
            | (hmac_result[offset + 2] & 0xFF) << 8
            | (hmac_result[offset + 3] & 0xFF)
        )
        result = str(binary % (10**TOTP_DIGITS)).zfill(TOTP_DIGITS)

        logger.debug(f"Generated TOTP code: {result}")

        return result
