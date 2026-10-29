# -*- coding: utf-8 -*-
"""
扫描工作目录里的素材，输出一份清单。

给 AI 用：在写分镜之前先跑这个，才知道有哪些片段可引用、各自多长、
什么画幅、有没有音轨。

用法：
    python scan.py                 # 扫当前目录
    python scan.py <素材目录>
    python scan.py <素材目录> --json       # 输出 JSON，便于程序处理
    python scan.py <素材目录> --describe   # 输出可填写的清单，请人/模型补"画面里是什么"
"""
import argparse
import json
import os
import sys

sys.dont_write_bytecode = True   # 工具目录保持只读，不生成 __pycache__

import jylib
from build import IMAGE_EXT, VIDEO_EXT, SKIP_DIRS, find_script

IMAGE_EXT_SET = IMAGE_EXT
DEFAULT_EST_RATE = 4.2   # 字/秒，和 build.py 的「估速」默认值保持一致


def collect(workdir):
    vids, imgs = [], []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            p = os.path.join(root, f)
            low = f.lower()
            if low.endswith(VIDEO_EXT):
                vids.append(p)
            elif low.endswith(IMAGE_EXT_SET):
                imgs.append(p)
    return sorted(vids), sorted(imgs)


def describe(path):
    try:
        m = jylib.ffprobe(path)
        return m
    except Exception:
        return {"duration": 0, "width": 0, "height": 0, "has_audio": False}


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0


def print_describe(data, est_rate=DEFAULT_EST_RATE, gap=0.25):
    """输出一份"人只要填空"的清单。

    AI 读不了画面时，这是最快的路径：让用户对着编号说一句内容，
    比让模型猜或者让用户学 script.md 语法都快。
    """
    print("素材清单 · 请给每段补一句「画面里是什么」，其余不用改")
    print("=" * 62)
    print()

    if not data["videos"]:
        print("（这个目录里没有视频素材）")
        print()

    for i, v in enumerate(data["videos"], 1):
        room = max(0, int((v["duration"] - gap) * est_rate))
        print("# %d  %s" % (i, v["path"]))
        print("   %5.2fs · %s · %s"
              % (v["duration"],
                 "%dx%d" % (v["width"], v["height"]) if v["width"] else "画幅未知",
                 "有音轨" if v["has_audio"] else "无音轨"))
        print("   可容纳 %d 字（超出会触发画面循环）" % room)
        print("   内容：")
        print()

    if data["images"]:
        print("-- 图片 --")
        for i, im in enumerate(data["images"], 1):
            print("# P%d %s" % (i, im["path"]))
            print("   用途：")
            print()

    print("=" * 62)
    print("填好后整段发回来即可。字数按 %.1f 字/秒估（偏保守，实测约 4.6；"
          % est_rate)
    print("想算得更紧就加 --est-rate 4.6。窄素材上宁可少写，超了会触发画面循环）。")


def main():
    ap = argparse.ArgumentParser(description="列出工作目录里的素材")
    ap.add_argument("workdir", nargs="?", default=".")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--describe", action="store_true",
                    help="输出可填写的清单（给每段补一句内容）")
    ap.add_argument("--est-rate", type=float, default=DEFAULT_EST_RATE,
                    help="估算语速，字/秒，默认 %.1f" % DEFAULT_EST_RATE)
    args = ap.parse_args()

    workdir = os.path.abspath(args.workdir)
    vids, imgs = collect(workdir)

    data = {"workdir": workdir, "videos": [], "images": [], "script": None,
            "total_seconds": 0.0}
    for p in vids:
        m = describe(p)
        data["videos"].append({
            "path": os.path.relpath(p, workdir).replace("\\", "/"),
            "name": os.path.basename(p),
            "duration": round(m["duration"], 3),
            "width": m["width"], "height": m["height"],
            "has_audio": m["has_audio"],
            "size": human_size(os.path.getsize(p)),
        })
        data["total_seconds"] += m["duration"]
    for p in imgs:
        data["images"].append({
            "path": os.path.relpath(p, workdir).replace("\\", "/"),
            "name": os.path.basename(p),
            "size": human_size(os.path.getsize(p)),
        })
    try:
        data["script"] = os.path.relpath(find_script(workdir), workdir).replace("\\", "/")
    except Exception:
        data["script"] = None

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return

    if args.describe:
        print_describe(data, est_rate=args.est_rate)
        return

    print("工作目录: %s" % workdir)
    print("脚本:     %s" % (data["script"] or "（没找到，请新建 script.md）"))
    print()
    print("视频 %d 个，合计 %.2f 秒" % (len(vids), data["total_seconds"]))
    if vids:
        print("%-32s %8s %-12s %6s %s" % ("文件", "时长", "画幅", "音轨", "大小"))
        print("-" * 76)
        for v in data["videos"]:
            print("%-32s %7.2fs %-12s %6s %s"
                  % (v["path"][:32], v["duration"],
                     "%dx%d" % (v["width"], v["height"]) if v["width"] else "?",
                     "有" if v["has_audio"] else "无", v["size"]))
    print()
    print("图片 %d 个" % len(imgs))
    for im in data["images"]:
        print("  %-40s %s" % (im["path"][:40], im["size"]))


if __name__ == "__main__":
    sys.exit(main())
