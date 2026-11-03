#!/usr/bin/env python3
"""Telegram YouTube 下载器：推送链接即下载到 NAS 指定目录，带菜单/反馈/字幕/去重。

功能：
- 路由：YouTube -> language，其它链接 -> youturb；前缀「指定」强制 youturb，前缀 language/语言 强制 language
- 三层去重：目录下载档案 + 成品文件 [id] 检查 + 并发 inflight 锁
- 实时进度：下载中每 2 秒编辑消息，带可视化进度条，遇到 Telegram 429 自动退避
- 「📥 正在下载」：列出全部进行中任务（排队中 / 下载中）与实时状态，可自动刷新
- 「❌ 失败任务」：记录全部失败任务（持久化到 JSON），可一键全部重新执行
- 字幕 429 限流自动降级为「无字幕」重试，避免整片下载失败
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, BotCommand,
)
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
import yt_dlp

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/mnt/nas-media/links/language")).expanduser()
YOUTUBE_DIR = Path(os.getenv("YOUTUBE_DIR", "/mnt/nas-media/links/youturb")).expanduser()
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
ARCHIVE_NAME = ".yt-dlp-archive.txt"
FAILED_STORE = Path(os.getenv("FAILED_STORE", "/opt/yt-tg-bot/failed.json"))
LOCK_FILE = os.getenv("LOCK_FILE", "/run/yt-tg-bot.lock")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("/var/log/yt-tg-bot.log", mode="a"),
              logging.StreamHandler()],
)
log = logging.getLogger("yt-tg")

URL_RE = re.compile(r"https?://\S+")
YT_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
sem = asyncio.Semaphore(MAX_CONCURRENT)
inflight = set()
inflight_lock = asyncio.Lock()
active_downloads = {}   # key -> {title, url, target_dir, state, progress, started}
active_lock = asyncio.Lock()
failed_downloads = []   # [{url, target_dir, title, error, timestamp}]
failed_lock = asyncio.Lock()

MAIN_MENU = ReplyKeyboardMarkup(
    [["📊 统计信息", "📁 文件管理"],
     ["📥 正在下载", "❌ 失败任务"],
     ["⚙️ 系统状态", "📥 使用说明"],
     ["❓ 帮助"]],
    resize_keyboard=True, is_persistent=True,
)
FILES_MENU = ReplyKeyboardMarkup(
    [["📋 最近 10 个", "📈 全部统计"],
     ["📥 正在下载", "❌ 失败任务"],
     ["🔙 返回主菜单"]],
    resize_keyboard=True, is_persistent=True,
)
STATS_MENU = ReplyKeyboardMarkup(
    [["📦 目录大小", "🧾 文件数量"],
     ["📥 正在下载", "❌ 失败任务"],
     ["🔙 返回主菜单"]],
    resize_keyboard=True, is_persistent=True,
)

NAS_LANGUAGE = "/volume1/media/links/language"
NAS_YOUTURB = "/volume1/media/links/youturb"

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi", ".m4v",
              ".mp3", ".m4a", ".opus", ".flac", ".wav"}


# ---------------------------------------------------------------- 基础工具
def is_allowed(uid: int) -> bool:
    return (not ADMIN_IDS) or uid in ADMIN_IDS


def human_size(b):
    if not b:
        return "未知"
    b = float(b)
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def fmt_duration(s):
    if not s:
        return "未知"
    m, sec = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in YT_HOSTS)


def progress_bar(pct, width=12):
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def dirs():
    return [DOWNLOAD_DIR, YOUTUBE_DIR]


# ---------------------------------------------------------------- 失败任务持久化
def load_failed():
    try:
        raw = json.loads(FAILED_STORE.read_text())
        if isinstance(raw, list):
            failed_downloads.clear()
            failed_downloads.extend(e for e in raw if isinstance(e, dict) and e.get("url"))
            log.info("loaded %d failed task(s) from %s", len(failed_downloads), FAILED_STORE)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("load failed store error: %s", e)


def save_failed():
    try:
        FAILED_STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FAILED_STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(failed_downloads, ensure_ascii=False, indent=1))
        os.replace(tmp, FAILED_STORE)
    except Exception as e:  # noqa: BLE001
        log.warning("save failed store error: %s", e)


async def record_failure(url, target_dir, title, error):
    async with failed_lock:
        for e in failed_downloads:
            if e.get("url") == url:
                e.update({"target_dir": str(target_dir), "title": title,
                          "error": str(error)[:300], "timestamp": time.time()})
                break
        else:
            failed_downloads.append({
                "url": url, "target_dir": str(target_dir), "title": title,
                "error": str(error)[:300], "timestamp": time.time(),
            })
        save_failed()
    log.warning("recorded failed task: %s (%s)", url, str(error)[:120])


async def clear_failure(url):
    async with failed_lock:
        before = len(failed_downloads)
        failed_downloads[:] = [e for e in failed_downloads if e.get("url") != url]
        if len(failed_downloads) != before:
            save_failed()


VID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")


def scan_incomplete():
    """扫描目录里未完成的下载（.part/.ytdl），用文件名中的视频 ID 还原成可重试任务。"""
    found = []
    for d in dirs():
        try:
            entries = list(d.iterdir())
        except FileNotFoundError:
            continue
        for p in entries:
            if not p.is_file():
                continue
            if p.name.endswith(".part") or p.name.endswith(".ytdl") or ".temp." in p.name:
                m = VID_RE.search(p.name)
                found.append({
                    "url": f"https://www.youtube.com/watch?v={m.group(1)}" if m else "",
                    "target_dir": str(d),
                    "title": p.name,
                    "error": "检测到未完成的下载文件（.part/.ytdl），可续传",
                    "timestamp": time.time(),
                })
    return found


async def scan_incomplete_into_failed():
    items = scan_incomplete()
    added = 0
    async with failed_lock:
        known = {e.get("url") for e in failed_downloads}
        for it in items:
            if not it["url"] or it["url"] in known:
                continue
            failed_downloads.append(it)
            known.add(it["url"])
            added += 1
        if added:
            save_failed()
    return added, len(items)


# ---------------------------------------------------------------- yt-dlp
def yt_opts(outdir=None, progress=None, subs=True):
    opts = {
        "quiet": True, "no_warnings": True,
        "retries": 10, "fragment_retries": 10,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "continuedl": True,
        "overwrites": False,
        "js_runtimes": {"node": {}}, "remote_components": ["ejs:github"],
        # 降低 YouTube 429（Too Many Requests）概率：请求间适当休眠
        "sleep_interval": 1,
        "max_sleep_interval": 5,
        "sleep_interval_subtitles": 2,
        "sleep_requests": 1,
    }
    if outdir is not None:
        base = {
            "outtmpl": str(outdir / "%(title).80s [%(id)s].%(ext)s"),
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "noplaylist": False,
            "download_archive": str(outdir / ARCHIVE_NAME),
            "progress_hooks": [lambda d: _hook(d, progress)],
        }
        if subs:
            base.update({
                "writesubtitles": True, "writeautomaticsub": True,
                "subtitleslangs": ["zh-Hans", "zh", "en"],
                "embedsubs": True, "subtitlesformat": "srt/best",
            })
        opts.update(base)
    return opts


def get_info(url):
    with yt_dlp.YoutubeDL(yt_opts()) as ydl:
        return ydl.extract_info(url, download=False)


def existing_file(outdir: Path, vid: str):
    if not vid:
        return None
    pat = f"[{vid}]"
    try:
        for p in outdir.iterdir():
            if not p.is_file() or p.name.startswith("."):
                continue
            if pat in p.name and p.suffix.lower() in VIDEO_EXTS:
                return p
    except FileNotFoundError:
        return None
    return None


def download_one(url, outdir, progress):
    # 字幕下载可能被 YouTube 限流（HTTP 429），失败时自动降级为「不带字幕」重试，
    # 避免整个视频下载失败。
    try:
        with yt_dlp.YoutubeDL(yt_opts(outdir, progress, subs=True)) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return True, "已在下载档案中，跳过", ""
            title = info.get("title", url)
            filename = ydl.prepare_filename(info)
        return True, title, filename
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "subtitle" in msg.lower() or "429" in msg:
            log.warning("subtitle download failed (%s); retrying without subtitles", msg[:200])
            progress["pct"] = 0.0
            progress["last"] = -1
            with yt_dlp.YoutubeDL(yt_opts(outdir, progress, subs=False)) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    return True, "已在下载档案中，跳过", ""
                title = info.get("title", url)
                filename = ydl.prepare_filename(info)
            return True, title + "（无字幕）", filename
        raise


def _hook(d, progress):
    if d["status"] == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes", 0)
        progress["pct"] = (done / total * 100) if total else 0.0
        progress["speed"] = d.get("speed")
        progress["status"] = "downloading"
    elif d["status"] == "finished":
        progress["pct"] = 100.0
        progress["status"] = "finished"


async def safe_edit(msg, text, send, **kw):
    """编辑消息；失败时退化为发送新消息，绝不抛异常打断流程。"""
    try:
        await msg.edit_text(text, **kw)
        return
    except (BadRequest, NetworkError, TelegramError) as e:
        log.warning("edit_text failed (%s); sending new message", e)
    except Exception as e:  # noqa: BLE001
        log.warning("edit_text unexpected (%s); sending new message", e)
    try:
        await send(text, **kw)
    except Exception as e:  # noqa: BLE001
        log.warning("reply_text failed: %s", e)


# ---------------------------------------------------------------- 文案
def system_text():
    import shutil
    lines = ["⚙️ 系统状态"]
    for d in dirs():
        files = [p for p in d.rglob("*") if p.is_file() and not p.name.startswith(".")]
        total = sum(p.stat().st_size for p in files)
        try:
            du = shutil.disk_usage(d)
            disk = f"磁盘剩余 {human_size(du.free)} / {human_size(du.total)}"
        except OSError:
            disk = "磁盘不可用"
        lines.append(f"{d}\n  文件 {len(files)} 个 / {human_size(total)} | {disk}")
    lines.append(f"并发上限 {MAX_CONCURRENT} | 进行中 {len(active_downloads)} | 失败 {len(failed_downloads)}")
    return "\n".join(lines)


def stats_text():
    lines = ["📊 已下载统计"]
    for d in dirs():
        files = [p for p in d.rglob("*") if p.is_file() and not p.name.startswith(".")]
        total = sum(p.stat().st_size for p in files)
        lines.append(f"• {d}: {len(files)} 个 / {human_size(total)}")
    return "\n".join(lines)


def list_text():
    files = []
    for d in dirs():
        files += [p for p in d.rglob("*") if p.is_file() and not p.name.startswith(".")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    files = files[:10]
    if not files:
        return "📁 暂无文件"
    lines = ["📁 最近 10 个文件:"]
    for p in files:
        lines.append(f"• {p.name} ({human_size(p.stat().st_size)})")
    return "\n".join(lines)


def active_text():
    tasks = list(active_downloads.values())
    if not tasks:
        return "📥 正在下载\n暂无正在下载的任务。"
    tasks.sort(key=lambda e: e.get("started", 0))
    lines = [f"📥 正在下载（{len(tasks)} 个）"]
    for i, e in enumerate(tasks, 1):
        pct = e["progress"].get("pct", 0.0)
        speed = e["progress"].get("speed")
        speedstr = f"  {human_size(speed)}/s" if speed else ""
        state = e.get("state", "准备中")
        elapsed = int(time.time() - e.get("started", time.time()))
        lines.append(
            f"{i}. {e['title']}\n"
            f"   {progress_bar(pct)} {pct:.1f}%{speedstr}\n"
            f"   状态: {state} | 已用 {elapsed}s\n"
            f"   目录: {e['target_dir']}"
        )
    return "\n".join(lines)


def failed_text():
    if not failed_downloads:
        return "❌ 失败任务\n暂无失败的下载任务。"
    lines = [f"❌ 失败任务（共 {len(failed_downloads)} 个）"]
    for i, e in enumerate(failed_downloads, 1):
        ts = time.strftime("%m-%d %H:%M", time.localtime(e.get("timestamp", 0)))
        lines.append(
            f"{i}. {e.get('title') or e['url']}\n"
            f"   目录: {e.get('target_dir', '?')}\n"
            f"   错误: {str(e.get('error', ''))[:120]}\n"
            f"   时间: {ts}"
        )
    lines.append("\n点下方按钮可全部重新执行，或发送 /retry 。")
    return "\n".join(lines)


def downloading_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 刷新", callback_data="downloading"),
         InlineKeyboardButton("❌ 失败任务", callback_data="failed")],
    ])


refreshers = {}   # key -> asyncio.Task，避免同一视图重复起刷新任务


async def live_downloads_view(edit_fn, key):
    """后台每 3 秒刷新「正在下载」视图，直到没有进行中任务（最多约 6 分钟）。"""
    if key in refreshers or not active_downloads:
        return

    async def run():
        idle = 0
        last = ""
        for _ in range(120):
            await asyncio.sleep(3)
            if not active_downloads:
                idle += 1
                if idle >= 2:
                    break
            else:
                idle = 0
            text = active_text()
            if text == last:
                continue
            last = text
            try:
                await edit_fn(text)
            except Exception:  # noqa: BLE001
                pass

    task = asyncio.create_task(run())
    refreshers[key] = task
    task.add_done_callback(lambda _t: refreshers.pop(key, None))


def failed_kb():
    if not failed_downloads:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 扫描未完成", callback_data="scan"),
             InlineKeyboardButton("🔄 刷新", callback_data="failed")],
            [InlineKeyboardButton("📥 正在下载", callback_data="downloading")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 全部重新执行", callback_data="retry_all")],
        [InlineKeyboardButton("🔎 扫描未完成", callback_data="scan"),
         InlineKeyboardButton("🗑 清空列表", callback_data="clear_failed")],
        [InlineKeyboardButton("🔄 刷新", callback_data="failed")],
    ])


# ---------------------------------------------------------------- 单条下载
async def process_one(url, forced, send):
    """下载一条链接。send(text, **kw) 负责发消息。返回 True 表示成功（含跳过）。"""
    target_dir = Path(forced) if forced else (DOWNLOAD_DIR if is_youtube(url) else YOUTUBE_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    msg = await send("🔍 获取视频信息...", reply_markup=MAIN_MENU)
    loop = asyncio.get_event_loop()

    # 元数据获取放在信号量之外，避免阻塞其它链接的响应
    try:
        info = await loop.run_in_executor(None, get_info, url)
    except Exception as e:  # noqa: BLE001
        log.warning("get_info failed: %s", e)
        await record_failure(url, target_dir, url, f"获取信息失败: {e}")
        await safe_edit(msg, f"❌ 获取信息失败: {str(e)[:300]}", send, reply_markup=MAIN_MENU)
        return False

    title = info.get("title", url)
    duration = info.get("duration")
    filesize = info.get("filesize") or info.get("filesize_approx")
    ext = info.get("ext") or "mp4"
    vid = info.get("id")
    entries = info.get("entries") or []
    extra = ""
    if info.get("_type") == "playlist":
        extra = f"\n📋 播放列表: {len(entries)} 个视频"

    # 去重 1：目录里已有该视频文件
    existed = existing_file(target_dir, vid) if vid else None
    if existed:
        await clear_failure(url)
        await safe_edit(msg, f"♻️ 已存在，跳过下载\n🎬 {existed.name}\n📁 {target_dir}",
                        send, reply_markup=MAIN_MENU)
        return True

    # 去重 2：同一条链接正在下载（并发消息）
    key = f"{target_dir}|{vid or url}"
    async with inflight_lock:
        if key in inflight:
            await safe_edit(msg, f"⏳ 该视频正在下载中，跳过重复请求\n🎬 {title}",
                            send, reply_markup=MAIN_MENU)
            return True
        inflight.add(key)

    # 注册到「正在下载」列表（排队中）
    async with active_lock:
        entry = {
            "title": title, "url": url, "target_dir": str(target_dir),
            "state": "排队中", "progress": {"pct": 0.0, "speed": None, "last": -1},
            "started": time.time(),
        }
        active_downloads[key] = entry

    try:
        async with sem:
            async with active_lock:
                entry["state"] = "下载中"
            await safe_edit(
                msg,
                f"📥 开始下载 -> {target_dir}\n🎬 {title}{extra}\n⏱ {fmt_duration(duration)}\n"
                f"📦 {human_size(filesize)}\n🎞 {ext}\n"
                f"{progress_bar(0)} 0%",
                send, reply_markup=MAIN_MENU,
            )

            progress = entry["progress"]
            poller = asyncio.create_task(_poll(msg, title, progress, send))
            try:
                ok, t, filename = await loop.run_in_executor(
                    None, download_one, url, target_dir, progress
                )
            except Exception as e:  # noqa: BLE001
                progress["done"] = True
                await poller
                log.warning("download failed: %s", e)
                await record_failure(url, target_dir, title, f"下载异常: {e}")
                await safe_edit(msg, f"❌ 下载异常: {str(e)[:300]}", send, reply_markup=MAIN_MENU)
                return False
            finally:
                async with inflight_lock:
                    inflight.discard(key)
                async with active_lock:
                    active_downloads.pop(key, None)
            progress["done"] = True
            await poller

        if not ok:
            await record_failure(url, target_dir, title, str(t))
            await safe_edit(msg, f"❌ 失败: {t}\n{t}", send, reply_markup=MAIN_MENU)
            return False

        await clear_failure(url)
        if not filename:
            await safe_edit(msg, f"♻️ 已下载过，跳过（下载档案已记录）\n🎬 {t}\n📁 {target_dir}",
                            send, reply_markup=MAIN_MENU)
        else:
            size = os.path.getsize(filename) if os.path.exists(filename) else 0
            await safe_edit(msg, f"✅ 完成\n🎬 {t}\n📁 {target_dir}/{Path(filename).name}\n📦 {human_size(size)}",
                            send, reply_markup=MAIN_MENU)
        return True
    finally:
        async with inflight_lock:
            inflight.discard(key)
        async with active_lock:
            active_downloads.pop(key, None)


async def _poll(msg, title, progress, send):
    progress["done"] = False
    backoff = 3.0
    last_text = ""
    while not progress.get("done"):
        await asyncio.sleep(backoff)
        if progress.get("done"):
            break
        pct = progress.get("pct", 0.0)
        if pct < 100 and abs(pct - progress.get("last", -1)) < 5:
            continue
        progress["last"] = pct
        speed = progress.get("speed")
        speedstr = f"  {human_size(speed)}/s" if speed else ""
        text = f"📥 下载中 {progress_bar(pct)} {pct:.1f}%{speedstr}\n🎬 {title}"
        if text == last_text:
            continue
        last_text = text
        try:
            await msg.edit_text(text)
            backoff = 3.0
        except Exception as e:  # noqa: BLE001
            err = str(e).lower()
            if "429" in err or "too many" in err:
                backoff = min(backoff * 2, 10.0)
            # "can't be edited" / "not modified"：静默跳过，不发新消息避免刷屏


# ---------------------------------------------------------------- 重试
async def retry_failed(send, urls=None):
    async with failed_lock:
        items = [dict(e) for e in failed_downloads if not urls or e.get("url") in urls]
    if not items:
        return 0, 0
    ok_n = 0
    for e in items:
        try:
            good = await process_one(e["url"], e.get("target_dir") or None, send)
        except Exception as ex:  # noqa: BLE001
            log.warning("retry failed for %s: %s", e["url"], ex)
            good = False
        if good:
            ok_n += 1
    return ok_n, len(items)


# ---------------------------------------------------------------- 命令
async def _plain(update, text, **kw):
    await update.message.reply_text(text, **kw)


async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "菜单"),
        BotCommand("status", "已下载统计"),
        BotCommand("stats", "已下载统计"),
        BotCommand("files", "文件列表"),
        BotCommand("downloading", "正在下载"),
        BotCommand("failed", "失败任务"),
        BotCommand("retry", "重试全部失败"),
        BotCommand("clearfailed", "清空失败列表"),
        BotCommand("scan", "扫描未完成下载"),
        BotCommand("sys", "系统状态"),
        BotCommand("usage", "使用说明"),
        BotCommand("help", "帮助"),
    ])


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("🚫 无权限", reply_markup=MAIN_MENU)
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 已下载统计", callback_data="stats"),
         InlineKeyboardButton("📁 文件列表", callback_data="list")],
        [InlineKeyboardButton("📥 正在下载", callback_data="downloading"),
         InlineKeyboardButton("❌ 失败任务", callback_data="failed")],
        [InlineKeyboardButton("❓ 帮助", callback_data="help")],
    ])
    await update.message.reply_text(
        "🎬 YouTube 下载器\n发送链接即下载到 NAS。YouTube 链接 -> language，其它链接 -> youturb。\n"
        "消息前加「指定」可强制存 youturb。支持多链接、播放列表、自动字幕；已下载过的视频会自动跳过。",
        reply_markup=kb,
    )
    await update.message.reply_text("👇 使用下方菜单", reply_markup=MAIN_MENU)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "使用方法：直接粘贴视频链接（可多条）。\n"
        "• YouTube 链接 -> NAS " + NAS_LANGUAGE + "\n"
        "• 其它链接 -> NAS " + NAS_YOUTURB + "\n"
        "• 消息前加「指定」强制存 youturb；加 language 强制存 language\n"
        "命令：/start 菜单 /status 统计 /files 文件列表 /downloading 正在下载\n"
        "     /failed 失败任务 /retry 重试全部失败 /clearfailed 清空 /scan 扫描未完成 /sys 系统状态\n"
        "同一视频重复发送会自动跳过，不会重复下载。",
        reply_markup=MAIN_MENU,
    )


async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats_text(), reply_markup=MAIN_MENU)


async def files_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(list_text(), reply_markup=MAIN_MENU)


async def show_downloads(update_or_msg, edit_key_prefix, chat_id):
    """发送「正在下载」视图并启动后台自动刷新；无任务时只回一次快照。"""
    msg = await update_or_msg.reply_text(active_text(), reply_markup=downloading_kb())
    if not active_downloads:
        return msg
    key = f"{edit_key_prefix}:{chat_id}"

    async def edit(text):
        await safe_edit(msg, text, msg.reply_text, reply_markup=downloading_kb())

    await live_downloads_view(edit, key)
    return msg


def chat_id_of(update) -> int:
    try:
        return int(update.effective_chat.id)
    except Exception:  # noqa: BLE001
        return 0


async def downloading_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_downloads(update.message, "cmd", chat_id_of(update))
    await update.message.reply_text("👇 菜单", reply_markup=MAIN_MENU)


async def failed_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(failed_text(), reply_markup=failed_kb())
    await update.message.reply_text("👇 菜单", reply_markup=MAIN_MENU)


def _chat_sender(ctx, chat_id):
    async def send(text, **kw):
        return await ctx.bot.send_message(chat_id, text, **kw)
    return send


async def retry_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("🚫 无权限", reply_markup=MAIN_MENU)
        return
    if not failed_downloads:
        await update.message.reply_text("❌ 失败任务\n暂无可重试的任务。", reply_markup=MAIN_MENU)
        return
    n = len(failed_downloads)
    await update.message.reply_text(f"🔁 开始重新执行 {n} 个失败任务...", reply_markup=MAIN_MENU)
    send = _chat_sender(ctx, chat_id_of(update))
    ok_n, total = await retry_failed(send)
    await update.message.reply_text(
        f"🔁 重试结束：成功 {ok_n}/{total}，仍失败 {len(failed_downloads)} 个。",
        reply_markup=MAIN_MENU,
    )


async def clearfailed_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    async with failed_lock:
        n = len(failed_downloads)
        failed_downloads.clear()
        save_failed()
    await update.message.reply_text(f"🗑 已清空 {n} 个失败任务记录。", reply_markup=MAIN_MENU)


async def scan_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    added, total = await scan_incomplete_into_failed()
    await update.message.reply_text(
        f"🔎 扫描完成：发现未完成文件 {total} 个，新加入失败列表 {added} 个。\n\n" + failed_text(),
        reply_markup=failed_kb(),
    )
    await update.message.reply_text("👇 菜单", reply_markup=MAIN_MENU)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "stats":
        await q.edit_message_text(stats_text(), reply_markup=q.message.reply_markup)
    elif data == "list":
        await q.edit_message_text(list_text(), reply_markup=q.message.reply_markup)
    elif data == "downloading":
        await q.edit_message_text(active_text(), reply_markup=downloading_kb())

        async def edit(text):
            try:
                await q.edit_message_text(text, reply_markup=downloading_kb())
            except Exception:  # noqa: BLE001
                pass

        await live_downloads_view(edit, f"cb:{q.message.chat_id}")
    elif data == "failed":
        await q.edit_message_text(failed_text(), reply_markup=failed_kb())
    elif data == "clear_failed":
        async with failed_lock:
            failed_downloads.clear()
            save_failed()
        await q.edit_message_text("🗑 失败任务已清空。", reply_markup=failed_kb())
    elif data == "scan":
        added, total = await scan_incomplete_into_failed()
        await q.edit_message_text(
            f"🔎 扫描完成：发现未完成文件 {total} 个，新加入失败列表 {added} 个。\n\n" + failed_text(),
            reply_markup=failed_kb(),
        )
    elif data == "retry_all":
        if not failed_downloads:
            await q.edit_message_text("❌ 暂无可重试的任务。", reply_markup=failed_kb())
            return
        n = len(failed_downloads)
        await q.edit_message_text(f"🔁 开始重新执行 {n} 个失败任务...", reply_markup=failed_kb())
        send = _chat_sender(ctx, q.message.chat_id)
        ok_n, total = await retry_failed(send)
        try:
            await q.edit_message_text(
                f"🔁 重试结束：成功 {ok_n}/{total}，仍失败 {len(failed_downloads)} 个。",
                reply_markup=failed_kb(),
            )
        except Exception:  # noqa: BLE001
            await ctx.bot.send_message(q.message.chat_id,
                                       f"🔁 重试结束：成功 {ok_n}/{total}，仍失败 {len(failed_downloads)} 个。")
    elif data == "help":
        await q.edit_message_text(
            "发送视频链接即可下载。\nYouTube -> language，其它 -> youturb；前加「指定」强制 youturb。\n"
            "已下载过的视频会自动跳过。\n命令: /status /files /downloading /failed /retry /sys",
            reply_markup=q.message.reply_markup,
        )


# ---------------------------------------------------------------- 消息路由
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("🚫 无权限", reply_markup=MAIN_MENU)
        return
    text = update.message.text or ""
    menu = ctx.user_data.get("menu", "main")
    if text == "🔙 返回主菜单":
        ctx.user_data["menu"] = "main"
        await update.message.reply_text("🏠 主菜单", reply_markup=MAIN_MENU)
        return

    # 「正在下载」「失败任务」在任何子菜单都直接响应
    if text == "📥 正在下载":
        await show_downloads(update.message, "menu", chat_id_of(update))
        return
    if text == "❌ 失败任务":
        await update.message.reply_text(failed_text(), reply_markup=failed_kb())
        return

    if menu == "main":
        if text == "📊 统计信息":
            ctx.user_data["menu"] = "stats"
            await update.message.reply_text(stats_text(), reply_markup=STATS_MENU)
            return
        if text == "📁 文件管理":
            ctx.user_data["menu"] = "files"
            await update.message.reply_text(list_text(), reply_markup=FILES_MENU)
            return
        if text == "⚙️ 系统状态":
            await update.message.reply_text(system_text(), reply_markup=MAIN_MENU)
            return
        if text == "❓ 帮助":
            await update.message.reply_text(
                "发送 YouTube/视频链接即可下载。\nYouTube -> language，其它 -> youturb；前加「指定」强制 youturb。\n"
                "命令: /status /files /downloading /failed /retry /sys /help",
                reply_markup=MAIN_MENU)
            return
        if text == "📥 使用说明":
            await update.message.reply_text(
                "直接粘贴视频链接，支持多链接与播放列表。\n"
                f"YouTube 视频存: NAS {NAS_LANGUAGE}\n"
                f"其它链接存: NAS {NAS_YOUTURB}\n"
                "消息前加「指定」可强制存 youturb；重复链接会自动跳过。",
                reply_markup=MAIN_MENU)
            return
    if menu == "files":
        if text == "📋 最近 10 个":
            await update.message.reply_text(list_text(), reply_markup=FILES_MENU)
            return
        if text == "📈 全部统计":
            await update.message.reply_text(stats_text(), reply_markup=FILES_MENU)
            return
    if menu == "stats":
        if text == "📦 目录大小":
            lines = ["📦 目录大小"]
            for d in dirs():
                files = [p for p in d.rglob("*") if p.is_file() and not p.name.startswith(".")]
                lines.append(f"• {d}: {human_size(sum(p.stat().st_size for p in files))} / {len(files)} 个")
            await update.message.reply_text("\n".join(lines), reply_markup=STATS_MENU)
            return
        if text == "🧾 文件数量":
            total = sum(1 for d in dirs() for p in d.rglob("*")
                        if p.is_file() and not p.name.startswith("."))
            await update.message.reply_text(f"🧾 文件数量: {total}", reply_markup=STATS_MENU)
            return

    # 路由：显式关键字优先，否则按链接来源判断
    # 默认：YouTube -> language；其它链接 -> youturb；消息前加「指定」强制存 youturb
    forced = None
    low = text.lstrip().lower()
    if low.startswith("指定"):
        forced = YOUTUBE_DIR
        text = re.sub(r"^\s*指定\s*", "", text, count=1)
    elif low.startswith("language") or low.startswith("语言"):
        forced = DOWNLOAD_DIR
        text = re.sub(r"(?i)^\s*(language|语言)\s*", "", text, count=1)
    elif low.startswith("youturb") or low.startswith("youtube") or re.match(r"^yt\s", low):
        forced = YOUTUBE_DIR
        text = re.sub(r"(?i)^\s*(youturb|youtube|yt)\s*", "", text, count=1)

    urls = URL_RE.findall(text)
    if not urls:
        await update.message.reply_text("请发送包含视频链接的消息。", reply_markup=MAIN_MENU)
        return

    async def send(msg_text, **kw):
        return await update.message.reply_text(msg_text, **kw)

    await asyncio.gather(*(process_one(u, forced, send) for u in urls),
                         return_exceptions=True)


# ---------------------------------------------------------------- 入口
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN 未设置")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    YOUTUBE_DIR.mkdir(parents=True, exist_ok=True)
    load_failed()

    # 单实例锁：避免多进程同时 getUpdates（409 Conflict）导致「正在下载」看到空列表
    try:
        import fcntl
        lock_fp = open(LOCK_FILE, "w")
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fp.write(str(os.getpid()))
        lock_fp.flush()
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"另一个实例正在运行（{LOCK_FILE}: {e}），退出")

    app = (Application.builder().token(BOT_TOKEN)
           .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
           .post_init(post_init)
           .build())

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("stats", status_cmd))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CommandHandler("downloading", downloading_cmd))
    app.add_handler(CommandHandler("failed", failed_cmd))
    app.add_handler(CommandHandler("retry", retry_cmd))
    app.add_handler(CommandHandler("clearfailed", clearfailed_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("sys", lambda u, c: u.message.reply_text(
        system_text(), reply_markup=MAIN_MENU)))
    app.add_handler(CommandHandler("usage", lambda u, c: u.message.reply_text(
        f"直接粘贴视频链接，支持多链接与播放列表。\n"
        f"YouTube -> NAS {NAS_LANGUAGE}\n其它 -> NAS {NAS_YOUTURB}\n"
        "消息前加「指定」强制 youturb；重复链接会自动跳过。",
        reply_markup=MAIN_MENU)))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def error_handler(update, context):
        log.error("Unhandled exception while processing update %s: %s",
                  getattr(update, "update_id", "?"), context.error, exc_info=context.error)

    app.add_error_handler(error_handler)

    log.info("bot started, download_dir=%s, youtube_dir=%s, pid=%s",
             DOWNLOAD_DIR, YOUTUBE_DIR, os.getpid())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
