# -*- coding: utf-8 -*-
"""
剪映配音回收：把草稿里「文本朗读」生成的语音抽出来，变成本地素材。

为什么要这一步：
    剪映朗读生成的 wav 落在 <草稿>/textReading/ 下，跟着草稿目录走。
    build.py 重跑会覆盖同名草稿，这批音频就没了；而且它们只在草稿目录里"存在"，
    换机器、剪映升级都可能失效。抽到工作目录之后三件事成立：
      1. 时长可以精确读出来（不用按字数估）
      2. 变成普通 wav，任何工具都能用
      3. 草稿被覆盖、剪映被卸载，音频都还在

数据结构（读 <草稿>/Timelines/<时间轴id>/draft_content.json，解密后）：
    materials.audios[]   其中 type == "text_to_audio" 的是朗读音频
        text_id     → 指向 materials.texts[].id
        path        "##_draftpath_placeholder_xxx_##/textReading/yyy.wav"，
                    去掉占位前缀后是相对草稿目录的路径
        duration    帧对齐后的真实时长（微秒），比 wav 实际长约 0~33ms
        tone_speaker / tone_effect_name   朗读用的音色
    materials.texts[].content 是 JSON 字符串，里面 "text" 才是字幕原文

注意：
    11.6 不再生成 template.json，早先那套「读明文模板」的做法已经失效。
    现在用 jycore 解密 draft_content.json，直接读内存里的结构，
    时间轴目录由 project.json 的 main_timeline_id 决定。
"""
import json
import os
import re
import shutil

import jycore

MANIFEST = "manifest.json"
LIB_DIRNAME = "剪映配音"

PLACEHOLDER = re.compile(r"##_draftpath_placeholder_[0-9A-Fa-f-]+_##")


# ---------------- 读草稿 ----------------

def _text_of(content):
    """text 素材的 content 是 JSON 字符串，取出纯文本。"""
    if not content:
        return ""
    try:
        return (json.loads(content).get("text") or "").strip()
    except (ValueError, AttributeError):
        return ""


def parse_voices(draft_dir):
    """从草稿里读出所有朗读音频。

    返回 [{"text":字幕原文, "wav":草稿内绝对路径, "duration":秒, "voice":音色}]
    读不出（草稿不存在 / 没朗读过）时返回空列表。
    """
    try:
        doc = jycore.read(jycore.content_path(draft_dir))
    except Exception:  # noqa: BLE001
        return []

    m = doc.get("materials") or {}
    texts = {t.get("id"): t for t in (m.get("texts") or [])}

    out = []
    for a in (m.get("audios") or []):
        if a.get("type") != "text_to_audio":
            continue
        t = texts.get(a.get("text_id"))
        if not t:
            continue
        text = _text_of(t.get("content"))
        if not text:
            continue
        rel = PLACEHOLDER.sub("", a.get("path") or "").lstrip("/").replace("/", os.sep)
        wav = os.path.normpath(os.path.join(draft_dir, rel))
        if not os.path.isfile(wav):
            continue
        out.append({
            "text": text,
            "wav": wav,
            "duration": round(a.get("duration", 0) / 1e6, 3),
            "voice": a.get("tone_effect_name") or a.get("tone_speaker") or "",
        })
    return out


# ---------------- 固化到工作目录 ----------------

def lib_dir(build_dir):
    """配音库位置：<工作目录>/.jybuild/剪映配音/"""
    return os.path.join(build_dir, LIB_DIRNAME)


def _read_manifest(dest):
    p = os.path.join(dest, MANIFEST)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return (json.load(f) or {}).get("voices") or {}
    except (OSError, ValueError):
        return {}


def _write_manifest(dest, voices, source=""):
    p = os.path.join(dest, MANIFEST)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({
            "version": 1,
            "source": source,
            "count": len(voices),
            "voices": voices,
        }, f, ensure_ascii=False, indent=1)


def harvest(draft_dir, build_dir, merge=True):
    """把草稿里的朗读音频复制到配音库，并更新 manifest。

    merge=True 时与已有库合并（同一句话以新读到的为准），
    这样多期视频的音频可以攒在同一个库房里复用。

    返回 (voices, stat)：
        voices  {字幕原文: {"wav": 相对文件名, "duration": 秒}}
        stat    {"found","copied","reused","merged"}
    """
    found = parse_voices(draft_dir)
    dest = lib_dir(build_dir)
    os.makedirs(dest, exist_ok=True)

    voices = _read_manifest(dest) if merge else {}
    before = len(voices)
    copied = reused = 0

    for v in found:
        name = os.path.basename(v["wav"])
        dst = os.path.join(dest, name)
        if os.path.isfile(dst) and os.path.getsize(dst) > 0:
            reused += 1
        else:
            shutil.copy2(v["wav"], dst)
            copied += 1
        voices[v["text"]] = {"wav": name, "duration": v["duration"]}

    if found:
        _write_manifest(dest, voices, source=os.path.abspath(draft_dir))

    return voices, {
        "found": len(found),
        "copied": copied,
        "reused": reused,
        "merged": len(voices) - before,
    }


def load(build_dir):
    """读配音库，返回 {字幕原文: (wav绝对路径, 秒数)}。文件丢了的条目自动跳过。"""
    dest = lib_dir(build_dir)
    raw = _read_manifest(dest)
    out = {}
    for text, v in raw.items():
        wav = os.path.join(dest, v.get("wav", ""))
        if os.path.isfile(wav) and os.path.getsize(wav) > 0:
            out[text] = (wav, float(v.get("duration") or 0))
    return out


def probe_missing(build_dir):
    """库里 manifest 有记录但文件缺失的条目数，用来提示用户。"""
    dest = lib_dir(build_dir)
    raw = _read_manifest(dest)
    miss = 0
    for v in raw.values():
        if not os.path.isfile(os.path.join(dest, v.get("wav", ""))):
            miss += 1
    return miss


# ---------------- 命令行 ----------------

def _main():
    import argparse

    ap = argparse.ArgumentParser(
        description="把剪映草稿里「文本朗读」生成的语音抽成本地素材")
    ap.add_argument("draft", nargs="?", help="草稿名或草稿目录的绝对路径")
    ap.add_argument("workdir", nargs="?", default=".",
                    help="工作目录（配音存到其 .jybuild/剪映配音/），默认当前目录")
    ap.add_argument("--list", action="store_true", help="只列配音库现状，不抽")
    ap.add_argument("--draft-root", help="剪映草稿根目录，仅当第一个参数是草稿名时用")
    args = ap.parse_args()

    import jylib

    # --list 时位置参数可能是目录（cuttts.py --list <目录>），也可能是草稿名，
    # 而 workdir 又省了默认值 "."，这里按"给了 draft 且没显式给 workdir"来让步
    workdir = args.workdir
    if args.list and workdir == "." and args.draft:
        workdir = args.draft
        args.draft = None

    workdir = os.path.abspath(workdir)
    build_dir = os.path.join(workdir, ".jybuild")

    if args.list:
        lib = load(build_dir)
        miss = probe_missing(build_dir)
        print("配音库: %s" % lib_dir(build_dir))
        print("可用条目: %d%s" % (len(lib), ("（另有 %d 条文件缺失）" % miss) if miss else ""))
        for text, (wav, dur) in list(lib.items())[:20]:
            print("  %6.2fs  %-34s %s" % (dur, text[:32], os.path.basename(wav)[:30]))
        return

    draft_dir = args.draft
    if not draft_dir:
        raise SystemExit("请给草稿名或草稿目录；只想看库现状请加 --list")
    if not os.path.isdir(draft_dir):
        root = args.draft_root or jylib.find_draft_root()
        draft_dir = os.path.join(root, args.draft)

    if not os.path.isdir(draft_dir):
        raise SystemExit("找不到草稿目录：%s" % draft_dir)

    voices, stat = harvest(draft_dir, build_dir)
    print("草稿: %s" % draft_dir)
    print("读出朗读音频 %d 条：新复制 %d，已存在 %d，库内共 %d 条"
          % (stat["found"], stat["copied"], stat["reused"], len(voices)))
    if not stat["found"]:
        print("没读到朗读音频。常见原因：")
        print("  1. 还没在剪映里对字幕执行「文本朗读」")
        print("  2. 朗读后剪映没保存就退出（草稿没落盘）")
        print("  3. 选错草稿了")
        return
    print("配音库: %s" % lib_dir(build_dir))


if __name__ == "__main__":
    _main()
