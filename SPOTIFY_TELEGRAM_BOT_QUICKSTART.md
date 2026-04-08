# Spotify Telegram Bot（M2）快速开始

## 1) 准备配置

复制示例配置：

```bash
cp ./spotify_bot.config.example.toml ./spotify_bot.config.toml
```

设置 Token（二选一）：

- 配置文件里填 `[bot].token`
- 或环境变量 `TELEGRAM_BOT_TOKEN`

---

## 2) 启动

```bash
cd /path/to/votify
source ./.venv/bin/activate
python -m votify.spotify_telegram_bot.main --config ./spotify_bot.config.toml
```

---

## 3) 可用命令

- `/h` / `/help` 帮助
- `/u <spotify-url>` 下载并上传
- `/sg <关键词>` 搜歌曲
- `/sa <关键词>` 搜专辑
- `/sr <关键词>` 搜艺人
- `/sp <关键词>` 搜歌单
- `/s <type> <关键词>` 统一搜索（`song|album|artist|playlist`）
- `/q` 查看队列状态

也支持直接发送 Spotify URL（等价于 `/u <url>`）。
搜索结果会带按钮，点击即可直接下载；支持上一页/下一页翻页。

---

## 4) 说明

- Bot 下载引擎复用 `votify`，实际行为由 `config.ini` 决定。
- 相同 URL 会写入并复用 `cache_file`（Telegram file_id 缓存），二次请求可直接回传。
- 默认启动时会清空历史未消费 update（避免重启后重复执行旧命令）。
- 可在 `spotify_bot.config.toml` 调整：
  - `max_parallel_jobs`（并发下载 worker）
  - `max_pending_jobs`（队列上限）
  - `download_timeout_sec`（单任务超时）
  - `download_retry_count`（下载失败自动重试次数）
  - `download_retry_backoff_sec`（重试退避秒数）
- 你当前稳定路线建议：
  - `session_type = web`
  - `audio_quality = aac-medium`
  - `wvd_path = samsung_sm-g398fn_16.0.0_a8886a0d_22589_l3.wvd`
