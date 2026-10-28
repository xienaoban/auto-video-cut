# -*- coding: utf-8 -*-
"""
草稿结构自检：轨道是否重叠/越界、素材文件是否还在、时长是否对得上。

用法：
    python validate.py <工作目录>              # 校验该目录 .jybuild/draft_content.json
    python validate.py <草稿名>                # 校验剪映草稿目录里同名的工程
    python validate.py <某个.json>
    python validate.py <工作目录> --json       # 机器可读

退出码：0=通过，1=有问题。AI 应当检查退出码。
"""
import json
import os
import sys

sys.dont_write_bytecode = True   # 工具目录保持只读，不生成 __pycache__

import jylib


def load(target, workdir=None):
    """返回 (显示名, 草稿 dict)。target 可以是 JSON 文件、草稿名、或工作目录。"""
    if target and os.path.isfile(target):
        return target, json.load(open(target, encoding="utf-8"))

    if target and os.path.isdir(target):
        p = os.path.join(target, ".jybuild", "draft_content.json")
        if os.path.exists(p):
            return p, json.load(open(p, encoding="utf-8"))
        raise SystemExit("该目录没有 .jybuild/draft_content.json：%s" % target)

    root = jylib.find_draft_root()
    if not root:
        raise SystemExit(jylib.DRAFT_ROOT_HINT)
    p = os.path.join(root, target or "", "draft_content.json")
    if not os.path.exists(p):
        raise SystemExit("找不到草稿: %s" % p)
    return p, json.load(open(p, encoding="utf-8"))


def check(d):
    problems = []
    mats = {}
    for slot, items in d.get("materials", {}).items():
        if isinstance(items, list):
            for m in items:
                if isinstance(m, dict) and m.get("id"):
                    mats[m["id"]] = (slot, m)

    info = {
        "canvas": "%sx%s" % (d.get("canvas_config", {}).get("width"),
                             d.get("canvas_config", {}).get("height")),
        "fps": d.get("fps"),
        "duration": d.get("duration", 0) / 1e6,
        "tracks": [],
    }

    for t in d.get("tracks", []):
        segs = sorted(t.get("segments", []),
                      key=lambda s: s.get("target_timerange", {}).get("start", 0))
        info["tracks"].append({"type": t.get("type"), "segments": len(segs)})
        prev_end = None
        for s in segs:
            tr = s.get("target_timerange") or {}
            st, du = tr.get("start", 0), tr.get("duration", 0)
            if du <= 0:
                problems.append("零长度片段 %s" % str(s.get("id"))[:8])
            if prev_end is not None and st < prev_end - 1000:
                problems.append("轨道重叠 @%.2fs" % (st / 1e6))
            prev_end = st + du

            _, m = mats.get(s.get("material_id"), (None, None))
            if m is None:
                problems.append("片段引用了不存在的素材 %s" % s.get("material_id"))
                continue
            src = m.get("path") or m.get("media_path") or ""
            if t.get("type") in ("video", "audio") and src and not os.path.exists(src):
                problems.append("素材文件缺失: %s" % src)
            mdur = m.get("duration", 0)
            srctr = s.get("source_timerange")
            if srctr and mdur and srctr.get("start", 0) + srctr.get("duration", 0) > mdur + 2000:
                problems.append("源区间越界 @%.2fs (%s)"
                                % (st / 1e6, os.path.basename(src)))
    return info, problems


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv
    target = args[0] if args else "."

    path, d = load(target)
    info, problems = check(d)

    if as_json:
        print(json.dumps({"file": path, "info": info, "problems": problems},
                         ensure_ascii=False, indent=1))
        return 1 if problems else 0

    print("草稿: %s" % path)
    print("画布: %s @ %sfps   总时长: %.3fs" % (info["canvas"], info["fps"], info["duration"]))
    print("轨道 %d 条:" % len(info["tracks"]))
    for t in info["tracks"]:
        print("  [%-6s] %2d 段" % (t["type"], t["segments"]))

    if problems:
        print("\n发现 %d 个问题:" % len(problems))
        for p in problems[:40]:
            print("  ! " + p)
        if len(problems) > 40:
            print("  ... 另有 %d 条" % (len(problems) - 40))
        return 1
    print("\nOK 结构自检通过：无越界、无重叠、素材齐全")
    return 0


if __name__ == "__main__":
    sys.exit(main())
