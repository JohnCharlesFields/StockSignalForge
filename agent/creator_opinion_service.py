"""Creator opinion ingestion for market-video research.

The module treats YouTube creators as a soft evidence source.  It stores raw
video metadata, transcripts when available, structured opinions, and sector
transmission summaries.  It never changes calibrated win rates directly.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests

from app_database import cache_get, cache_set, connection, ensure_database


AGENT_DIR = Path(__file__).resolve().parent
RUNS_DIR = AGENT_DIR / "runs"
COOKIES_FILE = Path(os.environ.get("YOUTUBE_COOKIES_FILE", AGENT_DIR / "secrets" / "youtube_cookies.txt"))
REQUEST_TIMEOUT = int(os.environ.get("CREATOR_OPINION_REQUEST_TIMEOUT", "35"))
TRANSCRIPT_MAX_CHARS = int(os.environ.get("CREATOR_TRANSCRIPT_MAX_CHARS", "18000"))
_JOB_LOCK = threading.Lock()
_JOB_ACTIVE = False
_STATUS_KEY = "creator_opinion:refresh:v1"


DEFAULT_CREATORS: list[dict[str, Any]] = [
    {"handle": "NaNaShuoMeiGu", "name": "NaNa说美股", "theme": "美股大盘 / AI产业链 / 期权结构"},
    {"handle": "TradesMax", "name": "TradesMax", "theme": "美股个股 / 财报交易"},
    {"handle": "SiliconValleyVector", "name": "SiliconValleyVector", "theme": "美股科技 / AI产业链"},
    {"handle": "ganzhi0906", "name": "ganzhi0906", "theme": "美股市场研究"},
    {"handle": "bellafinance", "name": "Bella Finance", "theme": "美股市场研究"},
    {"handle": "SUNNYFINANCE", "name": "Sunny Finance", "theme": "美股市场研究"},
    {"handle": "BeckieAnalysis", "name": "Beckie Analysis", "theme": "美股市场研究"},
]

TICKER_ALIASES: dict[str, list[str]] = {
    "NVDA": ["NVDA", "英伟达", "輝達", "NVIDIA"],
    "AAPL": ["AAPL", "苹果", "蘋果", "Apple"],
    "MSFT": ["MSFT", "微软", "微軟", "Microsoft"],
    "META": ["META", "Meta", "脸书", "Facebook"],
    "GOOGL": ["GOOGL", "GOOG", "谷歌", "Google"],
    "AMZN": ["AMZN", "亚马逊", "亞馬遜", "Amazon"],
    "TSLA": ["TSLA", "特斯拉", "Tesla"],
    "AVGO": ["AVGO", "博通", "Broadcom"],
    "AMD": ["AMD", "超微"],
    "MU": ["MU", "美光", "Micron"],
    "SNDK": ["SNDK", "闪迪", "閃迪", "Sandisk", "SanDisk"],
    "WDC": ["WDC", "西部数据", "Western Digital"],
    "PLTR": ["PLTR", "Palantir"],
    "RKLB": ["RKLB", "Rocket Lab", "小火箭"],
    "GLW": ["GLW", "康宁", "Corning"],
    "NOW": ["NOW", "ServiceNow"],
    "MSTR": ["MSTR", "MicroStrategy"],
    "SMH": ["SMH", "半导体ETF"],
    "SOXX": ["SOXX", "费半", "PHLX Semiconductor"],
    "QQQ": ["QQQ", "纳指", "纳斯达克"],
    "SPY": ["SPY", "标普", "标普500", "S&P 500"],
    "VIX": ["VIX", "恐慌指数"],
}

SECTOR_RULES: list[tuple[str, str, list[str]]] = [
    ("ai_infrastructure", "AI基础设施", ["AI产业链", "算力", "资本开支", "CapEx", "数据中心", "OpenAI", "Anthropic"]),
    ("semiconductor", "半导体", ["半导体", "费半", "芯片", "NVDA", "AVGO", "AMD", "台积电"]),
    ("memory_hbm", "存储/HBM", ["存储", "内存", "HBM", "MU", "美光", "SNDK", "闪迪", "WDC", "海力士", "三星"]),
    ("optical_networking", "光通信/网络设备", ["光模块", "光通信", "CPO", "玻璃基板", "GLW", "AAOI", "数据中心互连"]),
    ("mega_cap_tech", "科技巨头", ["七巨头", "MAG7", "MAGS", "苹果", "微软", "谷歌", "Meta", "亚马逊"]),
    ("software_ai", "AI软件", ["软件股", "NOW", "ServiceNow", "Palantir", "PLTR", "SaaS"]),
    ("space", "商业航天", ["SpaceX", "RKLB", "Rocket Lab", "商业航天", "火箭"]),
    ("crypto_equity", "加密相关股票", ["比特币", "MSTR", "Coinbase", "COIN", "矿股"]),
    ("macro_liquidity", "宏观流动性", ["美联储", "缩表", "降息", "流动性", "CPI", "PCE", "国债"]),
    ("index_gamma", "指数/Gamma结构", ["Gamma", "伽马", "SpotGamma", "做市商", "期权对冲", "VIX"]),
]

POSITIVE_TERMS = ["看涨", "看多", "强势", "利好", "反弹", "修复", "受益", "突破", "继续走高", "值得持续跟踪", "不贵"]
NEGATIVE_TERMS = ["看空", "利空", "风险", "下跌", "抛售", "破位", "暴跌", "回撤", "重挫", "放缓", "卖事实", "担忧"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _local_day(value):
    timestamp = _timestamp(value)
    return datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai")).date().isoformat() if timestamp else None


def creator_refresh_status():
    stored = cache_get(_STATUS_KEY) or {"status": "idle"}
    if stored.get("status") in {"queued", "running"} and not _JOB_ACTIVE:
        stored = {**stored, "status": "interrupted", "message": "上次更新被服务重启中断，可重新更新；已有视频保留"}
    if stored.get("status") == "completed" and (stored.get("pending_analysis") or any(
        video.get("opinion_available") is False
        for channel in stored.get("channels", []) for video in channel.get("videos", [])
    )):
        stored = {**stored, "status": "partial"}
    return {**stored, "auto_enabled": os.getenv("CREATOR_OPINION_AUTO_ENABLED", "1").lower() not in {"0", "false", "no"},
            "auto_interval_seconds": max(1800, int(os.getenv("CREATOR_OPINION_AUTO_INTERVAL_SECONDS", "3600"))),
            "max_analyses_per_run": max(1, min(10, int(os.getenv("CREATOR_OPINION_MAX_ANALYSES_PER_RUN", "2"))))}


def start_creator_refresh(*, handles=None, limit_per_channel=1, use_llm=True, force=False, automatic=False):
    global _JOB_ACTIVE
    with _JOB_LOCK:
        previous = cache_get(_STATUS_KEY) or {}
        if _JOB_ACTIVE:
            return {"started": False, **creator_refresh_status()}
        since = time.time() - (_timestamp(previous.get("finished_at")) or 0)
        interval = max(1800, int(os.getenv("CREATOR_OPINION_AUTO_INTERVAL_SECONDS", "3600"))) if automatic else 300
        if since < interval:
            return {"started": False, "status": "cooldown", "retry_after_seconds": int(interval - since)}
        _JOB_ACTIVE = True
        record = {"status": "queued", "started_at": _utc_now(), "automatic": automatic, "phase": "discover"}
        try:
            cache_set(_STATUS_KEY, record)
        except Exception:
            _JOB_ACTIVE = False
            raise

    def worker():
        global _JOB_ACTIVE
        try:
            config = {"handles": list(handles) if handles else None, "limit_per_channel": limit_per_channel,
                      "use_llm": use_llm, "force": force, "record": record}
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--refresh", _json_dump(config)],
                                       capture_output=True, text=True, encoding="utf-8",
                                       timeout=max(90, min(600, int(os.getenv("CREATOR_OPINION_JOB_TIMEOUT", "300")))))
            if completed.returncode:
                raise RuntimeError("creator_refresh_worker_failed")
            result = json.loads(completed.stdout.strip().splitlines()[-1])
            cache_set(_STATUS_KEY, {**record, **result})
        except subprocess.TimeoutExpired:
            partial = cache_get(_STATUS_KEY) or record
            cache_set(_STATUS_KEY, {**partial, "status": "partial", "finished_at": _utc_now(),
                                    "error": "refresh_timeout", "message": "本轮达到时间预算；已发现的视频保留，下轮继续提炼"})
        except Exception as exc:
            cache_set(_STATUS_KEY, {**record, "status": "failed", "finished_at": _utc_now(), "error": type(exc).__name__})
        finally:
            with _JOB_LOCK:
                _JOB_ACTIVE = False

    try:
        threading.Thread(target=worker, daemon=True, name="creator-opinion-refresh").start()
    except Exception:
        with _JOB_LOCK:
            _JOB_ACTIVE = False
        cache_set(_STATUS_KEY, {**record, "status": "failed", "finished_at": _utc_now()})
        raise
    return {"started": True, **record}


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_load(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _http() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 easymoneysniper/creator-opinion"})
    return session


def ensure_creator_tables() -> None:
    ensure_database()
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS creator_channels (
                handle TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                channel_id TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                theme TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                weight REAL NOT NULL DEFAULT 1.0,
                last_checked_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS creator_videos (
                video_id TEXT PRIMARY KEY,
                handle TEXT NOT NULL,
                channel_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL,
                transcript_status TEXT NOT NULL DEFAULT 'pending',
                opinion_status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_creator_videos_handle_published
            ON creator_videos(handle, published_at DESC);

            CREATE TABLE IF NOT EXISTS creator_transcripts (
                video_id TEXT PRIMARY KEY,
                language TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                method TEXT NOT NULL DEFAULT '',
                chars INTEGER NOT NULL DEFAULT 0,
                transcript_text TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS creator_opinions (
                video_id TEXT PRIMARY KEY,
                handle TEXT NOT NULL,
                published_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                model TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_creator_opinions_published
            ON creator_opinions(published_at DESC);

            CREATE TABLE IF NOT EXISTS creator_sector_signals (
                signal_id TEXT PRIMARY KEY,
                as_of_date TEXT NOT NULL,
                sector_key TEXT NOT NULL,
                sector_name TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_creator_sector_date
            ON creator_sector_signals(as_of_date DESC, sector_key);
            """
        )
        conn.commit()


def seed_default_channels() -> int:
    ensure_creator_tables()
    stamp = _utc_now()
    written = 0
    with connection() as conn:
        for item in DEFAULT_CREATORS:
            handle = item["handle"]
            cur = conn.execute(
                """
                INSERT INTO creator_channels(handle, name, url, theme, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(handle) DO UPDATE SET
                    name = COALESCE(NULLIF(creator_channels.name, ''), excluded.name),
                    url = COALESCE(NULLIF(creator_channels.url, ''), excluded.url),
                    theme = COALESCE(NULLIF(creator_channels.theme, ''), excluded.theme),
                    updated_at = creator_channels.updated_at
                """,
                (handle, item.get("name", handle), f"https://www.youtube.com/@{handle}", item.get("theme", ""), stamp, stamp),
            )
            written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    return written


def list_channels() -> list[dict[str, Any]]:
    seed_default_channels()
    with connection() as conn:
        rows = conn.execute("SELECT * FROM creator_channels ORDER BY enabled DESC, handle").fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["metadata"] = _json_load(item.pop("metadata_json", None), {})
        out.append(item)
    return out


def _resolve_channel_id(handle: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", handle):
        raise ValueError("invalid_channel_handle")
    url = f"https://www.youtube.com/@{handle}"
    response = _http().get(url, timeout=(5, min(15, REQUEST_TIMEOUT)))
    response.raise_for_status()
    text = response.text
    for pattern in (
        r'"externalId":"(UC[^"]+)"',
        r'<meta itemprop="channelId" content="(UC[^"]+)"',
        r'"browseId":"(UC[^"]+)"',
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    raise RuntimeError(f"channel_id_not_found:{handle}")


def _rss_latest(channel_id: str, limit: int = 3) -> list[dict[str, Any]]:
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
        raise ValueError("invalid_channel_id")
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    response = _http().get(url, timeout=(5, min(15, REQUEST_TIMEOUT)))
    response.raise_for_status()
    root = ET.fromstring(response.content)
    ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    videos: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns)[: max(1, int(limit))]:
        video_id = entry.findtext("yt:videoId", default="", namespaces=ns)
        title = entry.findtext("atom:title", default="", namespaces=ns)
        published = entry.findtext("atom:published", default="", namespaces=ns)
        link_el = entry.find("atom:link", ns)
        link = link_el.attrib.get("href", "") if link_el is not None else f"https://www.youtube.com/watch?v={video_id}"
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) and published:
            videos.append({"video_id": video_id, "title": title, "published_at": published, "url": link})
    return videos


def _upsert_channel(handle: str, *, channel_id: str = "", error: str = "") -> None:
    stamp = _utc_now()
    with connection() as conn:
        existing = conn.execute("SELECT name, theme, url FROM creator_channels WHERE handle=?", (handle,)).fetchone()
        name = existing["name"] if existing else handle
        theme = existing["theme"] if existing else ""
        url = existing["url"] if existing and existing["url"] else f"https://www.youtube.com/@{handle}"
        metadata = {"last_error": error} if error else {}
        conn.execute(
            """
            INSERT INTO creator_channels(handle, name, channel_id, url, theme, last_checked_at, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(handle) DO UPDATE SET
                channel_id = COALESCE(NULLIF(excluded.channel_id, ''), creator_channels.channel_id),
                last_checked_at = excluded.last_checked_at,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (handle, name, channel_id, url, theme, stamp, _json_dump(metadata), stamp, stamp),
        )
        conn.commit()


def _store_video(handle: str, channel_id: str, video: dict[str, Any]) -> bool:
    stamp = _utc_now()
    with connection() as conn:
        existed = conn.execute("SELECT video_id FROM creator_videos WHERE video_id=?", (video["video_id"],)).fetchone()
        conn.execute(
            """
            INSERT INTO creator_videos(video_id, handle, channel_id, title, url, published_at, fetched_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                handle = excluded.handle,
                channel_id = excluded.channel_id,
                title = excluded.title,
                url = excluded.url,
                published_at = excluded.published_at,
                fetched_at = excluded.fetched_at
            """,
            (
                video["video_id"],
                handle,
                channel_id,
                video.get("title", ""),
                video.get("url", ""),
                video.get("published_at", ""),
                stamp,
                _json_dump(video),
            ),
        )
        conn.commit()
    return not bool(existed)


def _load_video(video_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM creator_videos WHERE video_id=?", (video_id,)).fetchone()
    return dict(row) if row else None


class _CaptionLogger:
    def __init__(self):
        self.auth_required = False

    def debug(self, value):
        pass

    def info(self, value):
        pass

    def warning(self, value):
        text = str(value).lower()
        if "sign in to confirm" in text or "cookies are no longer valid" in text or "cookies have expired" in text:
            self.auth_required = True

    error = warning


def _extract_with_ytdlp(video_id: str) -> dict[str, Any]:
    try:
        import yt_dlp  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"yt_dlp_unavailable:{exc}") from exc

    logger = _CaptionLogger()
    opts_base = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": False,
        "logger": logger,
        "cookiefile": str(COOKIES_FILE) if COOKIES_FILE.exists() else None,
        "extract_flat": False,
        "ignore_no_formats_error": True,
        "socket_timeout": 10,
        "retries": 0,
        "extractor_retries": 0,
    }
    node = shutil.which("node")
    if node:
        opts_base["js_runtimes"] = {"node": {"path": node}}
    client_strategies = [None, ["tv"], ["web"], ["android"]]
    languages = ["zh-CN", "zh-Hans", "zh-Hant", "zh", "yue", "en", "en-US"]
    last_error = ""
    for client in client_strategies:
        opts = dict(opts_base)
        if client:
            opts["extractor_args"] = {"youtube": {"player_client": client}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            if logger.auth_required:
                raise RuntimeError("youtube_auth_required")
            subtitles = info.get("subtitles") or {}
            auto = info.get("automatic_captions") or {}
            for source_name, source in (("subtitles", subtitles), ("automatic_captions", auto)):
                for lang in list(dict.fromkeys(languages + [key for key in source if key.startswith(("zh", "en", "yue"))])):
                    rows = source.get(lang)
                    if not rows:
                        continue
                    item = next((x for x in rows if x.get("ext") == "json3"), None) or rows[0]
                    url = item.get("url")
                    if not url:
                        continue
                    return {"info": info, "language": lang, "source": source_name, "method": f"yt-dlp:{client or 'default'}", "url": url}
            last_error = f"no_subtitles:{','.join(list(subtitles.keys())[:8])}:{','.join(list(auto.keys())[:8])}"
        except Exception as exc:
            if logger.auth_required:
                raise RuntimeError("youtube_auth_required") from None
            last_error = f"{type(exc).__name__}:{str(exc)[:240]}"
    raise RuntimeError(last_error or "subtitle_url_unavailable")


def _download_subtitle_text(url: str) -> str:
    response = _http().get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    text_rows: list[str] = []
    if "json" in content_type or "fmt=json3" in url:
        data = response.json()
        for event in data.get("events", []):
            line = "".join(seg.get("utf8", "") for seg in (event.get("segs") or [])).strip()
            line = re.sub(r"\s+", " ", line)
            if line:
                text_rows.append(line)
        return html.unescape(" ".join(text_rows)).strip()
    raw = response.text
    raw = re.sub(r"WEBVTT|Kind:.*|Language:.*", "", raw)
    raw = re.sub(r"\d{2}:\d{2}:\d{2}[.,]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[.,]\d{3}.*", "", raw)
    raw = re.sub(r"\s+", " ", raw)
    return html.unescape(raw).strip()


def fetch_transcript(video_id: str, *, force: bool = False) -> dict[str, Any]:
    ensure_creator_tables()
    if not force:
        with connection() as conn:
            cached = conn.execute("SELECT * FROM creator_transcripts WHERE video_id=?", (video_id,)).fetchone()
        if cached:
            row = dict(cached)
            return {"available": True, **row}

    video = _load_video(video_id)
    try:
        meta = _bounded_transcript(video_id)
        if not meta.get("available"):
            raise RuntimeError(meta.get("error") or "subtitle_unavailable")
        text = meta["transcript_text"]
        if len(text) < 50:
            raise RuntimeError("transcript_too_short")
        if len(text) > TRANSCRIPT_MAX_CHARS:
            text = text[:TRANSCRIPT_MAX_CHARS] + "\n...TRUNCATED..."
        stamp = _utc_now()
        with connection() as conn:
            conn.execute(
                """
                INSERT INTO creator_transcripts(video_id, language, source, method, chars, transcript_text, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    language = excluded.language,
                    source = excluded.source,
                    method = excluded.method,
                    chars = excluded.chars,
                    transcript_text = excluded.transcript_text,
                    fetched_at = excluded.fetched_at
                """,
                (video_id, meta["language"], meta["source"], meta["method"], len(text), text, stamp),
            )
            conn.execute(
                "UPDATE creator_videos SET transcript_status=?, error=? WHERE video_id=?",
                ("ready", "", video_id),
            )
            conn.commit()
        return {
            "available": True,
            "video_id": video_id,
            "language": meta["language"],
            "source": meta["source"],
            "method": meta["method"],
            "chars": len(text),
            "transcript_text": text,
            "fetched_at": stamp,
        }
    except Exception as exc:
        detail = _transcript_error(exc)
        with connection() as conn:
            conn.execute(
                "UPDATE creator_videos SET transcript_status=?, error=? WHERE video_id=?",
                ("failed", detail, video_id),
            )
            conn.commit()
        return {"available": False, "video_id": video_id, "error": detail, "title": (video or {}).get("title", "")}


def _transcript_error(exc):
    value = str(exc).lower()
    if isinstance(exc, subprocess.TimeoutExpired) or "hard_timeout" in value:
        return "subtitle_timeout: 字幕提取超时，视频记录已保留"
    if "sign in" in value or "bot" in value or "youtube_auth_required" in value:
        return "youtube_auth_required: YouTube要求登录或人工验证；请在浏览器确认后更新授权Cookies"
    if "no_subtitles" in value or "subtitle_unavailable" in value:
        return "subtitle_unavailable: 当前未取得可用字幕，不能提炼视频观点"
    return f"{type(exc).__name__}: 字幕源暂不可用，视频记录已保留"


def _bounded_transcript(video_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("invalid_video_id")
    # yt-dlp may save its cookie jar on exit: only give it a disposable copy.
    with tempfile.TemporaryDirectory(prefix="creator-caption-") as directory:
        cookies = Path(directory) / "cookies.txt"
        if COOKIES_FILE.is_file():
            shutil.copy2(COOKIES_FILE, cookies)
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--transcript", video_id, str(cookies)],
                                capture_output=True, text=True, encoding="utf-8",
                                timeout=max(10, min(90, int(os.getenv("CREATOR_TRANSCRIPT_TIMEOUT", "60")))))
        if result.returncode:
            return {"available": False, "error": "subtitle_process_failed"}
        return json.loads(result.stdout.strip().splitlines()[-1])


def _mentions(text: str) -> list[str]:
    found: set[str] = set()
    for ticker, aliases in TICKER_ALIASES.items():
        for alias in aliases:
            if alias and alias in text:
                found.add(ticker)
                break
    for match in re.findall(r"\b[A-Z]{2,5}\b", text):
        if match in TICKER_ALIASES or match in {"AI", "VIX", "SPY", "QQQ", "MU"}:
            found.add(match)
    return sorted(found)


def _sector_hits(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key, name, terms in SECTOR_RULES:
        hits = [term for term in terms if term in text]
        if hits:
            out.append({"sector_key": key, "sector_name": name, "terms": hits[:8], "score": min(1.0, 0.25 + 0.12 * len(hits))})
    return out


def _window(text: str, token: str, radius: int = 80) -> str:
    idx = text.find(token)
    if idx < 0:
        return ""
    return text[max(0, idx - radius) : min(len(text), idx + len(token) + radius)]


def _stance_for_text(fragment: str) -> str:
    pos = sum(1 for term in POSITIVE_TERMS if term in fragment)
    neg = sum(1 for term in NEGATIVE_TERMS if term in fragment)
    if pos > neg + 1:
        return "bullish"
    if neg > pos + 1:
        return "bearish"
    if pos > neg:
        return "slightly_bullish"
    if neg > pos:
        return "slightly_bearish"
    return "neutral"


def _heuristic_opinion(video: dict[str, Any], transcript: dict[str, Any]) -> dict[str, Any]:
    title = str(video.get("title") or "")
    text = (title + "\n" + str(transcript.get("transcript_text") or "")).strip()
    mentioned = _mentions(text)
    sectors = _sector_hits(text)
    ticker_views = []
    for ticker in mentioned[:20]:
        aliases = TICKER_ALIASES.get(ticker, [ticker])
        frag = ""
        for alias in aliases:
            frag = _window(text, alias)
            if frag:
                break
        ticker_views.append({
            "ticker": ticker,
            "stance": _stance_for_text(frag),
            "confidence": 0.45 if frag else 0.25,
            "evidence": frag[:220],
        })
    sector_views = []
    for sector in sectors[:10]:
        fragments = [_window(text, term, radius=60) for term in sector.get("terms", [])[:3]]
        blob = " ".join(x for x in fragments if x)
        stance_context = f"{title} {blob or text[:600]}"
        sector_views.append({
            "sector_key": sector["sector_key"],
            "sector_name": sector["sector_name"],
            "stance": _stance_for_text(stance_context),
            "confidence": min(0.75, float(sector.get("score") or 0.4)),
            "leader_tickers": [t for t in mentioned if t not in {"SPY", "QQQ", "VIX"}][:8],
            "transmission": "leader_to_sector" if mentioned else "theme_discussion",
            "evidence": (blob or text[:240])[:280],
        })
    return {
        "available": bool(text),
        "status": "heuristic",
        "model": "rules",
        "creator": video.get("handle"),
        "video_id": video.get("video_id"),
        "title": video.get("title"),
        "published_at": video.get("published_at"),
        "summary": (text[:240] + "...") if len(text) > 240 else text,
        "macro_view": _stance_for_text(text),
        "ticker_views": ticker_views,
        "sector_views": sector_views,
        "risk_flags": [term for term in ["高位", "回撤", "破位", "流动性", "缩表", "财报", "卖事实"] if term in text][:8],
        "method_note": "规则兜底提炼；正式复核可启用 DeepSeek。",
    }


def _deepseek_opinion(video: dict[str, Any], transcript: dict[str, Any]) -> dict[str, Any]:
    text = (transcript.get("transcript_text") or "")[:TRANSCRIPT_MAX_CHARS]
    if not text:
        return _heuristic_opinion(video, transcript)
    if os.getenv("ENABLE_CREATOR_OPINION_LLM", "1").lower() in {"0", "false", "no"}:
        return _heuristic_opinion(video, transcript)
    try:
        from src.providers.chat import ChatLLM

        prompt = [
            {
                "role": "system",
                "content": (
                    "你是美股视频观点结构化分析器。只输出严格 JSON。"
                    "任务：从博主视频字幕中提炼个股观点、赛道观点、龙头到赛道的传导、风险和证据。"
                    "不要编造字幕中没有的信息。不要给确定性交易建议。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请按 schema 输出："
                    "{\"available\":true,\"summary\":\"...\",\"macro_view\":\"bullish|bearish|neutral|mixed\","
                    "\"ticker_views\":[{\"ticker\":\"MU\",\"stance\":\"bullish|bearish|neutral|mixed\","
                    "\"confidence\":0.0,\"horizon\":\"intraday|overnight|1-5d|swing|unknown\","
                    "\"evidence\":\"原文证据\",\"risk\":\"风险\"}],"
                    "\"sector_views\":[{\"sector_key\":\"memory_hbm\",\"sector_name\":\"存储/HBM\","
                    "\"stance\":\"bullish|bearish|neutral|mixed\",\"confidence\":0.0,"
                    "\"leader_tickers\":[\"MU\"],\"transmission\":\"leader_to_sector|sector_to_leader|macro_to_sector|none\","
                    "\"evidence\":\"原文证据\",\"risk\":\"风险\"}],"
                    "\"risk_flags\":[\"...\"],\"method_note\":\"...\"}\n\n"
                    f"视频元数据：{json.dumps(video, ensure_ascii=False)}\n\n字幕：\n{text}"
                ),
            },
        ]
        response = ChatLLM().chat(prompt, timeout=int(os.getenv("CREATOR_OPINION_LLM_TIMEOUT", "60")))
        raw = response.content or "{}"
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start : end + 1] if start >= 0 and end > start else raw)
        data.setdefault("available", True)
        data.setdefault("status", "llm_reviewed")
        data["model"] = os.getenv("LANGCHAIN_MODEL_NAME", "")
        data["creator"] = video.get("handle")
        data["video_id"] = video.get("video_id")
        data["title"] = video.get("title")
        data["published_at"] = video.get("published_at")
        return data
    except Exception as exc:
        data = _heuristic_opinion(video, transcript)
        data["llm_error"] = f"{type(exc).__name__}: 大模型提炼未完成，仅保留明确标注的字幕规则摘要"
        return data


def extract_opinion(video_id: str, *, use_llm: bool = True, force: bool = False) -> dict[str, Any]:
    ensure_creator_tables()
    if not force:
        with connection() as conn:
            cached = conn.execute("SELECT * FROM creator_opinions WHERE video_id=?", (video_id,)).fetchone()
        if cached:
            row = dict(cached)
            payload = _json_load(row.get("payload_json"), {})
            if payload.get("available"):
                return {**payload, "cached": True}
            # A failed result is not a permanent cache hit, nor a successful opinion.
            updated = _timestamp(row.get("updated_at"))
            with connection() as conn:
                recovered = conn.execute("SELECT chars FROM creator_transcripts WHERE video_id=?", (video_id,)).fetchone()
            if not recovered and updated and time.time() - updated < 3600:
                return {**payload, "available": False, "cached": True, "retry_after_seconds": 3600}

    video = _load_video(video_id)
    if not video:
        raise RuntimeError(f"video_not_found:{video_id}")
    # Prefer the transcript already cached by the refresh loop.  This avoids
    # hitting YouTube twice for the same video when force-refreshing opinions.
    transcript = fetch_transcript(video_id, force=False)
    if not transcript.get("available"):
        payload = {
            "available": False,
            "status": "transcript_unavailable",
            "creator": video.get("handle"),
            "video_id": video_id,
            "title": video.get("title"),
            "published_at": video.get("published_at"),
            "reason": transcript.get("error") or "transcript unavailable",
            "ticker_views": [],
            "sector_views": [],
        }
    else:
        payload = _deepseek_opinion(video, transcript) if use_llm else _heuristic_opinion(video, transcript)
    stamp = _utc_now()
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO creator_opinions(video_id, handle, published_at, status, model, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                status = excluded.status,
                model = excluded.model,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                video_id,
                video.get("handle", ""),
                video.get("published_at", ""),
                str(payload.get("status") or ("ready" if payload.get("available") else "failed")),
                str(payload.get("model") or ""),
                _json_dump(payload),
                stamp,
                stamp,
            ),
        )
        conn.execute(
            "UPDATE creator_videos SET opinion_status=? WHERE video_id=?",
            ("ready" if payload.get("available") else "failed", video_id),
        )
        conn.commit()
    return payload


def refresh_creator_opinions(
    *,
    handles: Iterable[str] | None = None,
    limit_per_channel: int = 1,
    use_llm: bool = True,
    force: bool = False,
    progress=None,
) -> dict[str, Any]:
    seed_default_channels()
    selected = {h.strip().lstrip("@") for h in handles or [] if h.strip()}
    channels = [c for c in list_channels() if c.get("enabled")]
    if selected:
        channels = [c for c in channels if c.get("handle") in selected]
    result = {"started_at": _utc_now(), "channels": [], "new_videos": 0, "opinions_ready": 0, "transcripts_ready": 0,
              "phase": "discover", "status": "running", "processed_channels": 0, "total_channels": len(channels)}
    candidates = []
    for channel in channels:
        handle = channel["handle"]
        item = {"handle": handle, "videos": [], "error": ""}
        try:
            channel_id = channel.get("channel_id") or _resolve_channel_id(handle)
            videos = _rss_latest(channel_id, limit=15)
            if not videos:
                raise RuntimeError("empty_public_video_feed")
            _upsert_channel(handle, channel_id=channel_id)
            for video in videos:
                is_new = _store_video(handle, channel_id, video)
                if is_new:
                    result["new_videos"] += 1
                item["videos"].append({**video, "is_new": is_new})
            candidates.extend(videos[:max(1, min(5, int(limit_per_channel)))])
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: 公开视频列表暂不可用"
            _upsert_channel(handle, error=item["error"])
        result["channels"].append(item)
        result["processed_channels"] += 1
        if progress:
            progress(result)
    # Metadata for every channel is visible before any slow transcript/LLM work.
    result["phase"] = "analyze"
    cursor = int(cache_get("creator_opinion:analysis_cursor:v1") or 0)
    candidates.sort(key=lambda v: v.get("published_at", ""), reverse=True)
    pending = []
    unavailable = 0
    for video in candidates:
        with connection() as conn:
            prior = conn.execute("SELECT payload_json, updated_at FROM creator_opinions WHERE video_id=?", (video["video_id"],)).fetchone()
        if prior and not force:
            payload = _json_load(prior["payload_json"], {})
            if payload.get("available"):
                continue
            with connection() as conn:
                recovered = conn.execute("SELECT chars FROM creator_transcripts WHERE video_id=?", (video["video_id"],)).fetchone()
            if not recovered and time.time() - (_timestamp(prior["updated_at"]) or 0) < 3600:
                unavailable += 1
                continue
        pending.append(video)
    if pending:
        cursor %= len(pending)
        pending = pending[cursor:] + pending[:cursor]
    maximum = max(1, min(10, int(os.getenv("CREATOR_OPINION_MAX_ANALYSES_PER_RUN", "2"))))
    for video in pending[:maximum]:
        if progress:
            result["current_video"] = video["title"]
            progress(result)
        try:
            opinion = extract_opinion(video["video_id"], use_llm=use_llm, force=force)
        except Exception as exc:
            opinion = {"available": False, "status": "unavailable", "reason": f"{type(exc).__name__}: 提炼暂未完成"}
        if opinion.get("available"):
            result["opinions_ready"] += 1
        else:
            unavailable += 1
        current = _load_video(video["video_id"]) or {}
        if current.get("transcript_status") == "ready":
            result["transcripts_ready"] += 1
        for channel in result["channels"]:
            for item in channel["videos"]:
                if item["video_id"] == video["video_id"]:
                    item.update(opinion_available=bool(opinion.get("available")), opinion_status=opinion.get("status"), reason=opinion.get("reason"))
    if pending:
        cache_set("creator_opinion:analysis_cursor:v1", cursor + maximum)
    result["pending_analysis"] = max(0, len(pending) - maximum)
    result["unavailable_opinions"] = unavailable
    failed = sum(bool(channel["error"]) for channel in result["channels"])
    result["status"] = "failed" if channels and failed == len(channels) else "partial" if failed or unavailable or result["pending_analysis"] else "completed"
    result["phase"] = "done"
    result.pop("current_video", None)
    result["finished_at"] = _utc_now()
    save_sector_signals_from_opinions()
    return result


def list_opinion_feed(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    ensure_creator_tables()
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT v.video_id, v.handle, v.title, v.url, v.published_at, v.transcript_status,
                   v.opinion_status, v.error, v.fetched_at, t.language, t.chars, o.payload_json
            FROM creator_videos v
            LEFT JOIN creator_transcripts t ON t.video_id = v.video_id
            LEFT JOIN creator_opinions o ON o.video_id = v.video_id
            ORDER BY datetime(v.published_at) DESC, v.video_id
            LIMIT ? OFFSET ?
            """,
            (max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
    items = []
    for row in rows:
        d = dict(row)
        d["opinion"] = _json_load(d.pop("payload_json", None), {})
        items.append(d)
    day_of = lambda item: _local_day(item.get("published_at"))
    latest_date = day_of(items[0]) if items else None
    previous_date = None
    if latest_date:
        for item in items:
            day = day_of(item)
            if day and day != latest_date:
                previous_date = day
                break
    return {
        "items": items,
        "latest_date": latest_date,
        "previous_date": previous_date,
        "latest_count": sum(1 for x in items if latest_date and day_of(x) == latest_date),
        "previous_count": sum(1 for x in items if previous_date and day_of(x) == previous_date),
    }


def _iter_recent_opinions(days: int = 3) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM creator_opinions
            WHERE published_at >= date('now', ?)
            ORDER BY published_at DESC
            """,
            (f"-{max(1, int(days))} days",),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        payload = _json_load(item.get("payload_json"), {})
        if payload and payload.get("available"):
            out.append(payload)
    return out


def save_sector_signals_from_opinions(days: int = 3) -> list[dict[str, Any]]:
    ensure_creator_tables()
    opinions = _iter_recent_opinions(days=days)
    grouped: dict[str, dict[str, Any]] = {}
    for opinion in opinions:
        for view in opinion.get("sector_views") or []:
            key = str(view.get("sector_key") or view.get("sector_name") or "").strip()
            if not key:
                continue
            bucket = grouped.setdefault(key, {
                "sector_key": key,
                "sector_name": view.get("sector_name") or key,
                "mentions": 0,
                "bullish": 0,
                "bearish": 0,
                "neutral": 0,
                "creators": set(),
                "leader_tickers": set(),
                "evidence": [],
            })
            bucket["mentions"] += 1
            stance = str(view.get("stance") or "neutral").lower()
            if "bull" in stance or "看多" in stance:
                bucket["bullish"] += 1
            elif "bear" in stance or "看空" in stance:
                bucket["bearish"] += 1
            else:
                bucket["neutral"] += 1
            if opinion.get("creator"):
                bucket["creators"].add(str(opinion.get("creator")))
            for ticker in view.get("leader_tickers") or []:
                bucket["leader_tickers"].add(str(ticker).upper())
            if view.get("evidence"):
                bucket["evidence"].append(str(view.get("evidence"))[:220])
    today = datetime.now(timezone.utc).date().isoformat()
    out: list[dict[str, Any]] = []
    with connection() as conn:
        for key, bucket in grouped.items():
            total = max(1, int(bucket["mentions"]))
            score = (bucket["bullish"] - bucket["bearish"]) / total
            payload = {
                **bucket,
                "creators": sorted(bucket["creators"]),
                "leader_tickers": sorted(bucket["leader_tickers"]),
                "evidence": bucket["evidence"][:5],
                "sentiment_score": round(score, 4),
                "stance": "bullish" if score > 0.25 else "bearish" if score < -0.25 else "mixed",
                "lookback_days": days,
            }
            payload.pop("sector_key", None)
            payload.pop("sector_name", None)
            signal_id = f"{today}:{key}"
            conn.execute(
                """
                INSERT INTO creator_sector_signals(signal_id, as_of_date, sector_key, sector_name, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at
                """,
                (signal_id, today, key, bucket["sector_name"], _json_dump(payload), _utc_now()),
            )
            out.append({"sector_key": key, "sector_name": bucket["sector_name"], **payload})
        conn.commit()
    return sorted(out, key=lambda x: (abs(float(x.get("sentiment_score") or 0)), int(x.get("mentions") or 0)), reverse=True)


def list_sector_signals(days: int = 7) -> list[dict[str, Any]]:
    ensure_creator_tables()
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM creator_sector_signals
            WHERE as_of_date >= date('now', ?)
            ORDER BY as_of_date DESC, sector_key
            """,
            (f"-{max(1, int(days))} days",),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        payload = _json_load(item.pop("payload_json", None), {})
        out.append({**item, **payload})
    return out


def creator_opinion_tags_for_symbol(symbol: str, days: int = 3) -> list[dict[str, Any]]:
    symbol = str(symbol or "").upper().strip()
    if not symbol:
        return []
    tags: list[dict[str, Any]] = []
    for opinion in _iter_recent_opinions(days=days):
        for view in opinion.get("ticker_views") or []:
            if str(view.get("ticker") or "").upper() == symbol:
                stance = str(view.get("stance") or "neutral")
                tags.append({
                    "label": f"博主观点:{'偏多' if 'bull' in stance else '偏空' if 'bear' in stance else '中性'}",
                    "tone": "good" if "bull" in stance else "bad" if "bear" in stance else "neutral",
                    "creator": opinion.get("creator"),
                    "video_id": opinion.get("video_id"),
                    "evidence": view.get("evidence"),
                })
    return tags[:5]


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--transcript":
    COOKIES_FILE = Path(sys.argv[3])
    try:
        meta = _extract_with_ytdlp(sys.argv[2])
        text = _download_subtitle_text(meta["url"])
        print(_json_dump({"available": True, "language": meta["language"], "source": meta["source"],
                          "method": meta["method"], "transcript_text": text[:TRANSCRIPT_MAX_CHARS]}))
    except Exception as exc:
        print(_json_dump({"available": False, "error": _transcript_error(exc)}))

if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--refresh":
    config = json.loads(sys.argv[2])
    record = config.pop("record")
    result = refresh_creator_opinions(**config, progress=lambda value: cache_set(_STATUS_KEY, {**record, **value, "status": "running"}))
    print(_json_dump(result))
