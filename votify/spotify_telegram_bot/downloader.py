from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from collections import deque
from dataclasses import dataclass
from time import monotonic
from pathlib import Path


PRIMARY_MEDIA_EXTS = {
    ".m4a",
    ".mp3",
    ".ogg",
    ".flac",
    ".mp4",
    ".webm",
}
ERROR_SUMMARY_RE = re.compile(r"Finished with (\d+) error\(s\)", re.IGNORECASE)
SKIPPING_RE = re.compile(r"Skipping .*?:\s*(.+)", re.IGNORECASE)


@dataclass(slots=True)
class DownloadResult:
    job_id: str
    output_dir: Path
    temp_dir: Path
    log_path: Path
    media_files: list[Path]


@dataclass(slots=True)
class VotifyRunError(RuntimeError):
    job_id: str
    output_dir: Path
    temp_dir: Path
    log_path: Path
    return_code: int
    log_tail: str

    def __str__(self) -> str:
        return (
            f"votify exited with code {self.return_code}\n"
            f"log: {self.log_path}\n"
            f"--- log tail ---\n{self.log_tail or 'No logs.'}"
        )


class VotifyRunner:
    def __init__(
        self,
        python_bin: str,
        votify_config_path: str,
        workdir: str,
        download_root: str,
        temp_root: str,
        download_timeout_sec: int = 7200,
        download_retry_count: int = 1,
        download_retry_backoff_sec: float = 2.0,
    ) -> None:
        self.python_bin = python_bin
        self.votify_config_path = votify_config_path
        self.workdir = workdir
        self.download_root = Path(download_root)
        self.temp_root = Path(temp_root)
        self.download_timeout_sec = max(60, int(download_timeout_sec))
        self.download_retry_count = max(0, int(download_retry_count))
        self.download_retry_backoff_sec = max(0.0, float(download_retry_backoff_sec))

    async def download_url(self, url: str) -> DownloadResult:
        max_attempts = 1 + self.download_retry_count
        last_error: VotifyRunError | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return await self._download_url_once(url)
            except VotifyRunError as exc:
                last_error = exc
                should_retry = attempt < max_attempts and self._is_retryable_error(exc)
                if not should_retry:
                    raise
                # 失败尝试的产物立即清理，避免长期运行占满磁盘
                self.cleanup(
                    DownloadResult(
                        job_id=exc.job_id,
                        output_dir=exc.output_dir,
                        temp_dir=exc.temp_dir,
                        log_path=exc.log_path,
                        media_files=[],
                    )
                )
                backoff = self.download_retry_backoff_sec * attempt
                if backoff > 0:
                    await asyncio.sleep(backoff)

        # 正常不会到这里
        if last_error is not None:
            raise last_error
        raise RuntimeError("download_url reached an unexpected state")

    async def _download_url_once(self, url: str) -> DownloadResult:
        job_id = uuid.uuid4().hex
        job_root = self.download_root / job_id
        output_dir = job_root / "output"
        temp_dir = self.temp_root / job_id
        log_path = job_root / "votify.log"

        output_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            self.python_bin,
            "-m",
            "votify",
            "--config-path",
            self.votify_config_path,
            "--output",
            str(output_dir),
            "--temp",
            str(temp_dir),
            "--overwrite",
            url,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        log_tail = deque(maxlen=30)
        assert proc.stdout is not None
        deadline = monotonic() + self.download_timeout_sec
        with log_path.open("w", encoding="utf-8") as lf:
            try:
                while True:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    line = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=remaining,
                    )
                    if not line:
                        break
                    s = line.decode("utf-8", errors="replace")
                    lf.write(s)
                    log_tail.append(s)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise VotifyRunError(
                    job_id=job_id,
                    output_dir=output_dir,
                    temp_dir=temp_dir,
                    log_path=log_path,
                    return_code=-9,
                    log_tail=(
                        "".join(log_tail).strip()
                        or "votify timeout exceeded; process killed."
                    ),
                ) from None

        return_code = await proc.wait()

        if return_code != 0:
            raise VotifyRunError(
                job_id=job_id,
                output_dir=output_dir,
                temp_dir=temp_dir,
                log_path=log_path,
                return_code=return_code,
                log_tail="".join(log_tail),
            )

        media_files = sorted(
            [
                p
                for p in output_dir.rglob("*")
                if p.is_file() and p.suffix.lower() in PRIMARY_MEDIA_EXTS
            ],
            key=lambda p: str(p).lower(),
        )

        if not media_files:
            # votify 在部分失败场景下进程仍可能返回 0，这里通过日志补充判定。
            try:
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                log_text = ""
            m = ERROR_SUMMARY_RE.search(log_text)
            if m and int(m.group(1)) > 0:
                raise VotifyRunError(
                    job_id=job_id,
                    output_dir=output_dir,
                    temp_dir=temp_dir,
                    log_path=log_path,
                    return_code=return_code,
                    log_tail="No media files produced; votify reported errors.",
                )
            m_skip = SKIPPING_RE.search(log_text)
            if m_skip:
                reason = m_skip.group(1).strip()
                raise VotifyRunError(
                    job_id=job_id,
                    output_dir=output_dir,
                    temp_dir=temp_dir,
                    log_path=log_path,
                    return_code=return_code,
                    log_tail=f"No media files produced; {reason}",
                )

        return DownloadResult(
            job_id=job_id,
            output_dir=output_dir,
            temp_dir=temp_dir,
            log_path=log_path,
            media_files=media_files,
        )

    @staticmethod
    def _is_retryable_error(exc: VotifyRunError) -> bool:
        text = (exc.log_tail or "").lower()
        if exc.return_code == -9:
            return True
        retry_patterns = (
            "unexpected_eof_while_reading",
            "connection reset",
            "temporarily unavailable",
            "temporary failure",
            "timed out",
            "timeout",
            "network is unreachable",
            "http error 429",
            "too many requests",
            "retrying",
        )
        return any(p in text for p in retry_patterns)

    def cleanup(self, result: DownloadResult) -> None:
        job_root = result.output_dir.parent
        if job_root.exists():
            shutil.rmtree(job_root, ignore_errors=True)
        if result.temp_dir.exists():
            shutil.rmtree(result.temp_dir, ignore_errors=True)
