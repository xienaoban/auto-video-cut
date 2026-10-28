# -*- coding: utf-8 -*-
"""
列出可用的配音音色。

用法：
    python voices.py         # 只看中文音色
    python voices.py --all   # 全部音色
"""
import asyncio
import os
import sys

sys.dont_write_bytecode = True   # 工具目录保持只读，不生成 __pycache__

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, ".vendor")
if os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)


async def main():
    try:
        import edge_tts
    except ImportError:
        sys.exit("edge-tts 不可用（.vendor 缺失），无法查询音色")

    voices = await edge_tts.list_voices()
    show_all = "--all" in sys.argv
    rows = [v for v in voices if show_all or v.get("ShortName", "").startswith("zh-CN")]
    rows.sort(key=lambda v: v["ShortName"])
    if not rows:
        sys.exit("没有匹配的音色")

    print("%-26s %-8s %s" % ("ShortName", "性别", "说明"))
    print("-" * 74)
    for v in rows:
        traits = ",".join(v.get("VoiceTag", {}).get("VoicePersonalities", []) or [])
        print("%-26s %-8s %s" % (v["ShortName"], v.get("Gender", ""), traits))
    print("-" * 74)
    print("共 %d 个。把 ShortName 填到 script.md 的「音色:」后面。" % len(rows))


if __name__ == "__main__":
    asyncio.run(main())
