# -*- coding: utf-8 -*-
"""
环境自检。开跑前先来一遍，比生成到一半报错省事。

用法：
    python doctor.py
    python doctor.py --json

退出码：0=全部正常，1=有阻塞问题。
"""
import json
import os
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True   # 工具目录保持只读，不生成 __pycache__

import jylib

ASSETS_NEEDED = ("skeleton.json", "video_material.json", "text_material.json",
                 "audio_material.json", "video_segment.json", "text_segment.json",
                 "audio_segment.json")


def has_cmd(name):
    return shutil.which(name) is not None


def ffmpeg_version():
    try:
        p = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        return p.stdout.splitlines()[0] if p.stdout else "?"
    except Exception:
        return None


def jianying_running():
    """剪映运行时锁着草稿目录，必须在生成前退出。"""
    if os.name != "nt":
        return False
    try:
        p = subprocess.run(["tasklist"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        low = (p.stdout or "").lower()
        return "jianying" in low or "capcut" in low
    except Exception:
        return False


def main():
    rows = []

    def add(name, ok, detail, blocking=True):
        rows.append({"item": name, "ok": ok, "detail": detail,
                     "blocking": blocking})

    py = "%d.%d.%d" % sys.version_info[:3]
    add("Python", sys.version_info >= (3, 8), py)

    ver = ffmpeg_version()
    add("ffmpeg", bool(ver), ver or "未找到，请安装 ffmpeg 并加入 PATH")
    add("ffprobe", has_cmd("ffprobe"), shutil.which("ffprobe") or "未找到")

    try:
        import tts
        ok = tts.available()
        add("edge-tts", ok,
            ("已就绪 (%s)" % os.path.join(".vendor")) if ok
            else "未安装，配音会退回「剪映朗读」模式（按字数估算时长）",
            blocking=False)
    except Exception as e:
        add("edge-tts", False, "导入失败: %s" % e, blocking=False)

    missing = [a for a in ASSETS_NEEDED
               if not os.path.exists(os.path.join(jylib.ASSETS, a))]
    add("字段模板 assets/", not missing,
        "齐全" if not missing else "缺失: " + ", ".join(missing))

    root = jylib.find_draft_root()
    add("剪映草稿目录", bool(root), root or "未找到，需要配置 config.json 的 draft_root")

    running = jianying_running()
    add("剪映进程", not running,
        "剪映正在运行，生成前请完全退出（含托盘 JianyingProTray.exe）" if running
        else "未运行，可以安全写入")

    font = None
    for c in jylib.FONT_FALLBACKS:
        if os.path.exists(c):
            font = c
            break
    add("中文字体兜底", bool(font), font or "未找到系统中文无衬线字体", blocking=False)

    if "--json" in sys.argv:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        print("环境自检\n" + "-" * 68)
        for r in rows:
            mark = "OK  " if r["ok"] else ("FAIL" if r["blocking"] else "WARN")
            print("[%s] %-16s %s" % (mark, r["item"], r["detail"]))
        bad = [r for r in rows if not r["ok"] and r["blocking"]]
        print("-" * 68)
        print("阻塞问题 %d 个" % len(bad) if bad else "全部正常，可以生成草稿")

    return 1 if any(not r["ok"] and r["blocking"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
