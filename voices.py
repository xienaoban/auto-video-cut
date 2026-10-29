# -*- coding: utf-8 -*-
"""
列出可用的配音音色。

用法：
    python voices.py         # 全部中文音色（zh-CN / zh-HK / zh-TW）
    python voices.py --all   # 所有语言的音色
"""
import asyncio
import os
import sys

sys.dont_write_bytecode = True   # 工具目录保持只读，不生成 __pycache__

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, ".vendor")
if os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)


ZH_PREFIXES = ("zh-CN", "zh-HK", "zh-TW")


def _locale(name):
    parts = name.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else name


async def main():
    try:
        import edge_tts
    except ImportError:
        sys.exit("edge-tts 不可用（.vendor 缺失），无法查询音色")

    voices = await edge_tts.list_voices()
    show_all = "--all" in sys.argv
    if show_all:
        rows = list(voices)
    else:
        # 全部中文音色。只匹配 zh-CN 会漏掉 zh-HK / zh-TW 共 6 个
        rows = [v for v in voices if v.get("ShortName", "").startswith(ZH_PREFIXES)]
    if not rows:
        sys.exit("没有匹配的音色")

    def loc(v):
        return v.get("Locale") or _locale(v["ShortName"])

    rows.sort(key=lambda v: (loc(v), v["ShortName"]))

    cur = None
    for v in rows:
        here = loc(v)
        if here != cur:
            cur = here
            if here != loc(rows[0]):
                print()
            print("── %s（%d 个）" % (here, sum(1 for x in rows if loc(x) == here)))
            print("%-28s %-6s %s" % ("ShortName", "性别", "说明"))
            print("-" * 78)
        traits = ",".join(v.get("VoiceTag", {}).get("VoicePersonalities", []) or [])
        print("%-28s %-6s %s" % (v["ShortName"], v.get("Gender", ""), traits))
    print("-" * 78)
    print("共 %d 个。把 ShortName 填到 script.md 的「音色:」后面。" % len(rows))
    print("注意：整片只能用一个音色（build.py 一次性合成全部字幕）。")


if __name__ == "__main__":
    asyncio.run(main())
