"""全面验证 yt-tg-bot：路由 / 去重 / 菜单 / 命令 / 异常处理。
在 LXC104 容器内以真实 handler 调用，不使用网络 mock。
"""
import asyncio
import os
import sys

sys.path.insert(0, "/opt/yt-tg-bot")
import bot  # noqa: E402

YT = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
OTHER = "https://download.samplelib.com/mp4/sample-5s.mp4"

results = []


class Sent:
    def __init__(self, store):
        self.store = store

    async def edit_text(self, text, **kw):
        self.store.append(("edit", text))
        return self


class Msg:
    def __init__(self, store, text):
        self.store = store
        self.text = text

    async def reply_text(self, text, **kw):
        self.store.append(("reply", text))
        return Sent(self.store)


class User:
    id = 111
    username = "tester"


class Upd:
    def __init__(self, text):
        self.store = []
        self.effective_user = User()
        self.message = Msg(self.store, text)
        self.callback_query = None


class Ctx:
    def __init__(self):
        self.user_data = {}


def joined(u):
    return "\n".join(t for _, t in u.store)


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(detail)[:160]) if detail else ""))
    return cond


async def send(text, ctx=None):
    u = Upd(text)
    c = ctx or Ctx()
    await bot.handle_message(u, c)
    return u, joined(u)


TEST_IDS = ("jNQXAC9IVRw", "sample-5s")


def purge_test_artifacts():
    """清掉历次测试留下的文件与档案行，保证每次从零开始。"""
    for d in (bot.DOWNLOAD_DIR, bot.YOUTUBE_DIR):
        for name in os.listdir(d):
            if any(f"[{i}]" in name for i in TEST_IDS):
                try:
                    os.remove(os.path.join(d, name))
                except OSError as e:
                    print("  purge warn", name, e)
        a = os.path.join(d, bot.ARCHIVE_NAME)
        if os.path.exists(a):
            keep = [l for l in open(a).read().splitlines()
                    if not any(i in l for i in TEST_IDS)]
            open(a, "w").write("\n".join(keep) + ("\n" if keep else ""))
    bot.failed_downloads.clear()


async def main():
    print("### 0. 清理历次测试产物")
    purge_test_artifacts()

    print("\n### A. 纯函数 / 路由判定")
    check("is_youtube: youtube.com", bot.is_youtube("https://www.youtube.com/watch?v=x"))
    check("is_youtube: youtu.be", bot.is_youtube("https://youtu.be/x"))
    check("is_youtube: m.youtube.com", bot.is_youtube("https://m.youtube.com/watch?v=x"))
    check("is_youtube: youtube-nocookie", bot.is_youtube("https://www.youtube-nocookie.com/embed/x"))
    check("is_youtube: bilibili 非 YouTube", not bot.is_youtube("https://www.bilibili.com/video/BV1xx"))
    check("is_youtube: 直链非 YouTube", not bot.is_youtube(OTHER))

    print("\n### B. 目录配置")
    check("DOWNLOAD_DIR = language", str(bot.DOWNLOAD_DIR) == "/mnt/nas-media/links/language", bot.DOWNLOAD_DIR)
    check("YOUTUBE_DIR = youturb", str(bot.YOUTUBE_DIR) == "/mnt/nas-media/links/youturb", bot.YOUTUBE_DIR)
    check("language 目录存在", bot.DOWNLOAD_DIR.is_dir())
    check("youturb 目录存在", bot.YOUTUBE_DIR.is_dir())

    print("\n### C. 真实下载 + 路由")
    u, j = await send(YT)
    check("YouTube -> language 并下载成功",
          "-> /mnt/nas-media/links/language" in j and "✅ 完成" in j, j.replace("\n", " / ")[:150])

    u, j = await send(OTHER)
    check("其它链接 -> youturb 并下载成功",
          "-> /mnt/nas-media/links/youturb" in j and "✅ 完成" in j, j.replace("\n", " / ")[:150])

    print("\n### D. 去重（第二次发送）")
    import time

    def hit(j):
        return "已存在，跳过" in j or "跳过" in j

    u, j = await send(YT)
    check("重复 YouTube 链接被跳过", hit(j), j.replace("\n", " / ")[:120])
    t0 = time.time()
    u, j = await send(OTHER)
    dt = time.time() - t0
    check("重复直链被跳过（需先取元数据，允许 <30s）", hit(j) and dt < 30, f"{dt:.2f}s | " + j.replace("\n", " / ")[:100])

    print("\n### E. 强制路由")

    def routed(j, path):
        # 下载时是 "-> 路径"，命中已有文件时是 "📁 路径"
        return f"-> {path}" in j or f"📁 {path}" in j

    u, j = await send("指定 " + YT)
    check("「指定」+ YouTube -> youturb", routed(j, "/mnt/nas-media/links/youturb"), j.replace("\n", " / ")[:150])
    u, j = await send("language " + OTHER)
    check("「language」+ 直链 -> language", routed(j, "/mnt/nas-media/links/language"), j.replace("\n", " / ")[:150])
    u, j = await send("youturb " + OTHER)
    check("「youturb」+ 直链 -> youturb", routed(j, "/mnt/nas-media/links/youturb"), j.replace("\n", " / ")[:150])
    u, j = await send("语言 " + OTHER)
    check("「语言」+ 直链 -> language", routed(j, "/mnt/nas-media/links/language"), j.replace("\n", " / ")[:150])

    print("\n### F. 非法输入")
    u, j = await send("你好，没有链接")
    check("无链接给出提示", "请发送包含视频链接" in j, j.replace("\n", " / ")[:100])

    print("\n### G. 菜单（每个子菜单用独立 ctx，按状态机顺序点）")
    seq = [
        # 主菜单分支（各自独立 ctx，起点必须是 main）
        ("main", "⚙️ 系统状态", "系统状态"),
        ("main", "❓ 帮助", "YouTube"),
        ("main", "📥 使用说明", "直接粘贴视频链接"),
        # 统计子菜单：从 main 点「📊 统计信息」进入，再点子项
        ("main", "📊 统计信息", "已下载统计"),
        ("stats", "📦 目录大小", "目录大小"),
        ("stats", "🧾 文件数量", "文件数量"),
        ("stats", "🔙 返回主菜单", "主菜单"),
        # 文件子菜单：从 main 点「📁 文件管理」进入
        ("main", "📁 文件管理", "最近 10 个文件"),
        ("files", "📋 最近 10 个", "文件"),
        ("files", "📈 全部统计", "已下载统计"),
        ("files", "🔙 返回主菜单", "主菜单"),
    ]
    for start_state, label, expect in seq:
        ctx = Ctx()
        ctx.user_data["menu"] = start_state
        try:
            u, j = await send(label, ctx)
            check(f"菜单[{start_state}]「{label}」", expect in j,
                  f"next_menu={ctx.user_data.get('menu')} | " + j.replace("\n", " / ")[:80])
        except Exception as e:  # noqa: BLE001
            check(f"菜单[{start_state}]「{label}」", False, f"EXC {e}")

    # 状态机跳转校验
    ctx = Ctx()
    ctx.user_data["menu"] = "main"
    await send("📊 统计信息", ctx)
    check("状态跳转 main -> stats", ctx.user_data.get("menu") == "stats", ctx.user_data)
    await send("🔙 返回主菜单", ctx)
    check("状态跳转 stats -> main", ctx.user_data.get("menu") == "main", ctx.user_data)
    await send("📁 文件管理", ctx)
    check("状态跳转 main -> files", ctx.user_data.get("menu") == "files", ctx.user_data)
    await send("🔙 返回主菜单", ctx)
    check("状态跳转 files -> main", ctx.user_data.get("menu") == "main", ctx.user_data)

    print("\n### H. 命令处理")
    for fn, name, expect in [
        (bot.start, "/start", "YouTube 下载器"),
        (bot.help_cmd, "/help", "使用方法"),
        (bot.status_cmd, "/status", "已下载统计"),
        (bot.files_cmd, "/files", "文件"),
    ]:
        try:
            u = Upd("")
            await fn(u, Ctx())
            check(f"命令 {name}", expect in joined(u), joined(u).replace("\n", " / ")[:90])
        except Exception as e:  # noqa: BLE001
            check(f"命令 {name}", False, f"EXC {e}")

    print("\n### I. 文件系统落地结果")
    lang = sorted(p for p in os.listdir(bot.DOWNLOAD_DIR) if not p.startswith("."))
    yo = sorted(p for p in os.listdir(bot.YOUTUBE_DIR) if not p.startswith("."))
    print("language:", lang)
    print("youturb:", yo)
    check("language 含 YouTube 测试视频", any("[jNQXAC9IVRw]" in f for f in lang))
    check("youturb 含直链测试视频", any("[sample-5s]" in f for f in yo))
    check("youturb 含「指定」路由的 YouTube 视频", any("[jNQXAC9IVRw]" in f for f in yo))
    arch_lang = open(os.path.join(bot.DOWNLOAD_DIR, bot.ARCHIVE_NAME)).read()
    check("language 档案记录测试视频", "jNQXAC9IVRw" in arch_lang, arch_lang.strip().replace("\n", " | ")[:120])

    print("\n### J. 清理测试产物（精确子串匹配）")
    for d in (bot.YOUTUBE_DIR, bot.DOWNLOAD_DIR):
        for name in os.listdir(d):
            if "[jNQXAC9IVRw]" in name or "[sample-5s]" in name:
                os.remove(os.path.join(d, name))
                print("  RM", os.path.join(str(d), name))
        a = os.path.join(d, bot.ARCHIVE_NAME)
        if os.path.exists(a):
            keep = [l for l in open(a).read().splitlines()
                    if "jNQXAC9IVRw" not in l and "sample-5s" not in l]
            open(a, "w").write("\n".join(keep) + ("\n" if keep else ""))
    lang = sorted(p for p in os.listdir(bot.DOWNLOAD_DIR) if not p.startswith("."))
    yo = sorted(p for p in os.listdir(bot.YOUTUBE_DIR) if not p.startswith("."))
    print("清理后 language:", lang)
    print("清理后 youturb:", yo)
    check("清理后 language 无测试产物",
          not any(any(f"[{i}]" in f for i in TEST_IDS) for f in lang), lang[:3])
    check("清理后 youturb 无测试产物",
          not any(any(f"[{i}]" in f for i in TEST_IDS) for f in yo), yo[:3])

    print("\n### K. 失败任务记录 / 重试 / 清空")
    bot.failed_downloads.clear()

    def fake_fail(url, err="HTTP Error 429: Too Many Requests"):
        bot.failed_downloads.append({
            "url": url, "target_dir": str(bot.DOWNLOAD_DIR),
            "title": "失败样本", "error": err, "timestamp": time.time(),
        })

    fake_fail("https://example.com/a")
    fake_fail("https://example.com/b", "ERROR: unable to download video data")
    check("failed_text 列出全部失败任务",
          "共 2 个" in bot.failed_text() and "example.com/a" not in bot.failed_text()
          and "失败样本" in bot.failed_text(), bot.failed_text().replace("\n", " / ")[:120])
    kb = bot.failed_kb()
    labels = [b.text for row in kb.inline_keyboard for b in row]
    check("失败任务键盘含「全部重新执行」", any("全部重新执行" in x for x in labels), labels)
    check("失败任务键盘含「清空列表」", any("清空列表" in x for x in labels), labels)

    for label, expect in [("❌ 失败任务", "失败任务"),
                          ("📥 正在下载", "正在下载")]:
        for state in ("main", "files", "stats"):
            ctx = Ctx()
            ctx.user_data["menu"] = state
            u, j = await send(label, ctx)
            check(f"菜单[{state}]「{label}」", expect in j, j.replace("\n", " / ")[:80])

    u = Upd("")
    await bot.failed_cmd(u, Ctx())
    check("命令 /failed", "失败任务" in joined(u), joined(u).replace("\n", " / ")[:80])

    u = Upd("")
    await bot.downloading_cmd(u, Ctx())
    check("命令 /downloading", "正在下载" in joined(u), joined(u).replace("\n", " / ")[:80])

    u = Upd("")
    await bot.clearfailed_cmd(u, Ctx())
    check("命令 /clearfailed 清空记录",
          not bot.failed_downloads and "已清空" in joined(u), joined(u).replace("\n", " / ")[:80])

    fake_fail("https://example.com/c")
    u = Upd("")
    await bot.retry_cmd(u, Ctx())
    check("命令 /retry 会执行并汇总结果",
          "重试结束" in joined(u) or "开始重新执行" in joined(u), joined(u).replace("\n", " / ")[:120])
    bot.failed_downloads.clear()

    check("active_text 空态", "暂无正在下载" in bot.active_text())
    bot.active_downloads["k1"] = {
        "title": "任务A", "url": "u", "target_dir": "/x",
        "state": "下载中", "progress": {"pct": 42.0, "speed": 1048576}, "started": time.time() - 9,
    }
    bot.active_downloads["k2"] = {
        "title": "任务B", "url": "u2", "target_dir": "/y",
        "state": "排队中", "progress": {"pct": 0.0, "speed": None}, "started": time.time() - 3,
    }
    at = bot.active_text()
    check("active_text 列出全部进行中任务（2 个）",
          "（2 个）" in at and "任务A" in at and "任务B" in at, at.replace("\n", " / ")[:140])
    check("active_text 含进度条与速度",
          "█" in at and "MB/s" in at, at.replace("\n", " / ")[:120])
    bot.active_downloads.clear()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    failed = [n for n, ok in results if not ok]
    print(f"TOTAL {len(results)}  PASS {passed}  FAIL {len(results) - passed}")
    if failed:
        print("FAILED:", failed)
    print("RESULT", "ALL_PASS" if not failed else "SOME_FAIL")


asyncio.run(main())
