# -*- coding: utf-8 -*-
"""
文案驱动剪辑：script.md → 剪映草稿

本脚本是「只读工具」：工具目录本身不写入任何东西（配音/转码缓存都落在
工作目录的 .jybuild/ 里）。所以本目录可以整体搬到任何位置，也可以被多个
项目同时引用。

用法：
    python build.py                      # 工作目录 = 当前目录
    python build.py <素材目录>            # 指定工作目录
    python build.py <素材目录> --dry-run  # 只打印分镜表，不动任何文件
    python build.py <素材目录> --script 稿子.md --name 我的片名
    python build.py <素材目录> --draft-root "D:/剪映草稿"

两种配音模式（脚本头部 config 里的「配音」）：
    edge    用 edge-tts 本地合成，拿真实音频时长，字幕与音频边界严格重合（推荐）
    剪映    不合成音频，按字数估算时长，音频留给你在剪映里用「文本朗读」生成

设计要点：
1. 长文案先按标点切成短字幕，每条字幕单独合成语音，因此字幕起止与音频起止天然一致。
2. 视频片段按「同一素材连续截取」取用，取完再回头循环，避免画面重复。
3. 分镜槽位 = 配音总长 + 留白，留白用来吃掉句间的呼吸感。
"""
import argparse
import json
import os
import re
import sys

sys.dont_write_bytecode = True   # 工具目录保持只读，不生成 __pycache__

import cuttts
import jylib
import prep
from jylib import Draft, US

BUILD_DIR = ".jybuild"          # 工作目录内的中间产物目录
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
SKIP_DIRS = {BUILD_DIR, ".git", "__pycache__", "node_modules"}

DEFAULTS = {
    "title": "",
    "size": "1920x1080",
    "fps": "30",
    "tts": "edge",              # edge / 剪映
    "voice": "zh-CN-XiaoyiNeural",
    "tts_rate": "+0%",
    "est_rate": "4.2",          # 仅「剪映」模式：估算语速 字/秒
    "gap": "0.25",              # 分镜间隙（秒）
    "subtitle_y": "-0.78",
    "font_size": "15",
    "color": "#ffffff",
    "stroke": "#000000",
}

PUNCT = "，。！？；、,.!?;"


# ---------------------------------------------------------------- 解析脚本
def parse_script(path):
    with open(path, encoding="utf-8") as f:
        raw = f.read()

    cfg = dict(DEFAULTS)
    shots = []
    cur = None
    in_header = False

    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if s == "---":
            in_header = not in_header
            continue

        if s.startswith("#"):
            cur = {"文案": "", "素材": "", "图片": "", "起点": None, "时长": None,
                   "不字幕": False, "字号": None, "位置Y": None}
            shots.append(cur)
            tail = s.lstrip("#").strip()
            # 「# 3」这种纯编号只作分隔，不当字幕
            if tail and not tail.isdigit():
                cur["文案"] = tail
            continue

        m = re.match(r"^([^：:]{1,6})[：:]\s*(.*)$", s)
        if not m:
            continue
        k, v = m.group(1).strip(), m.group(2).strip()

        if cur is None or in_header:
            if k in ("标题", "title"):
                cfg["title"] = v
            elif k in ("尺寸", "画幅", "size"):
                cfg["size"] = v
            elif k in ("帧率", "fps"):
                cfg["fps"] = v
            elif k in ("配音", "tts"):
                cfg["tts"] = v
            elif k in ("音色", "voice"):
                cfg["voice"] = v
            elif k in ("语速", "rate"):
                if v.endswith("%"):
                    cfg["tts_rate"] = v
                else:
                    cfg["est_rate"] = v
            elif k in ("估速",):
                cfg["est_rate"] = v
            elif k in ("留白", "gap"):
                cfg["gap"] = v
            elif k in ("字幕位置", "subtitle_y"):
                cfg["subtitle_y"] = v
            elif k in ("字号", "font_size"):
                cfg["font_size"] = v
            elif k in ("颜色", "color"):
                cfg["color"] = v
            elif k in ("描边", "stroke"):
                cfg["stroke"] = v
            continue

        if k in ("文案", "口播"):
            cur["文案"] = v
        elif k in ("素材", "视频"):
            cur["素材"] = v
        elif k in ("图片", "配图"):
            cur["图片"] = v
        elif k == "起点":
            cur["起点"] = float(v)
        elif k == "时长":
            cur["时长"] = float(v)
        elif k == "不字幕":
            cur["不字幕"] = v not in ("0", "false", "否", "")
        elif k == "字号":
            cur["字号"] = float(v)
        elif k in ("位置Y", "位置"):
            cur["位置Y"] = float(v)

    # 只要有画面（文案/图片/素材任一），就是有效分镜。
    # 「# 3」这种纯编号分隔行三项都空，会被过滤掉。
    # 只写「素材 + 不字幕 + 时长」的空镜必须留下，否则过渡镜头会凭空消失。
    return cfg, [s for s in shots if s["文案"] or s["图片"] or s["素材"]]


# ---------------------------------------------------------------- 字幕切分
SENT_END = "。！？!?"
SOFT = "，、；,;"
ASCII_TOKEN = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_-.%")


def _hard_split(seg, max_len):
    """超长句：优先在标点处断，其次避开 ASCII 词（input/clips 这类）从中间断。"""
    out, s = [], seg
    while len(s) > max_len:
        cut = -1
        for i in range(max_len, max(5, max_len // 2), -1):
            if s[i - 1] in SOFT:
                cut = i
                break
        if cut < 0:
            cut = max_len
            # 不要把一个 ASCII 词劈成两半
            while cut > 5 and s[cut - 1] in ASCII_TOKEN and s[cut] in ASCII_TOKEN:
                cut -= 1
        out.append(s[:cut].strip())
        s = s[cut:].strip()
    if s:
        out.append(s)
    return out


def split_subtitle(text, max_len=20):
    """按句子 → 标点 → 兜底的顺序切分，尽量让每条字幕是一个完整语义单元。"""
    text = text.strip()
    if not text:
        return []
    # 1. 先按句末标点切
    sentences, buf = [], ""
    for ch in text:
        buf += ch
        if ch in SENT_END:
            sentences.append(buf.strip())
            buf = ""
    if buf.strip():
        sentences.append(buf.strip())

    # 2. 过长句再按逗号切，仍过长则兜底
    pieces = []
    for s in sentences:
        if len(s) <= max_len:
            pieces.append(s)
        else:
            sub, buf = [], ""
            for ch in s:
                buf += ch
                if ch in SOFT and len(buf) >= 8:
                    sub.append(buf.strip())
                    buf = ""
            if buf.strip():
                sub.append(buf.strip())
            for x in sub:
                pieces.extend([x] if len(x) <= max_len else _hard_split(x, max_len))

    # 3. 把过短的碎片并回前一条，避免屏幕上闪一下
    merged = []
    for p in pieces:
        if merged and (len(p) <= 4 or len(merged[-1]) <= 4) \
                and len(merged[-1]) + len(p) <= max_len + 6:
            merged[-1] += p
        else:
            merged.append(p)
    return merged or [text]


def clean_len(text):
    return len(re.sub(r"[\s%s]" % re.escape(PUNCT), "", text)) or 1


# ---------------------------------------------------------------- 路径显示
def rel(p, base):
    """显示用的相对路径。跨盘符时 relpath 会抛错，退化成绝对路径。"""
    try:
        return os.path.relpath(p, base)
    except ValueError:
        return p


# ---------------------------------------------------------------- 工作目录
def walk_files(workdir, exts):
    """递归收集工作目录下的素材，跳过隐藏目录和构建目录。"""
    found = []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.lower().endswith(exts) and not f.startswith("."):
                found.append(os.path.join(root, f))
    return sorted(found)


def resolve_asset(name, workdir, exts=None):
    """把脚本里的相对文件名解析成真实路径。支持子目录、省略扩展名。"""
    name = name.strip().strip('"').strip("'")
    if os.path.isabs(name):
        if os.path.exists(name):
            return name
        raise FileNotFoundError("素材不存在: %s" % name)

    direct = os.path.join(workdir, name.replace("\\", "/"))
    if os.path.exists(direct):
        return direct
    # 省略扩展名
    if exts:
        for e in exts:
            if os.path.exists(direct + e):
                return direct + e
    # 同名文件在子目录里
    base = os.path.basename(name)
    for p in walk_files(workdir, exts or ()):
        if os.path.basename(p) == base:
            return p
    if exts:
        stem = os.path.splitext(base)[0]
        for p in walk_files(workdir, exts):
            if os.path.splitext(os.path.basename(p))[0] == stem:
                return p
    raise FileNotFoundError(
        "找不到素材「%s」。工作目录里可用的素材请用 scan.py 查看。" % name)


def find_script(workdir, explicit=None):
    if explicit:
        p = explicit if os.path.isabs(explicit) else os.path.join(workdir, explicit)
        if not os.path.exists(p):
            raise FileNotFoundError("找不到脚本: %s" % p)
        return p
    for n in ("script.md", "分镜.md", "文案.md", "脚本.md", "SCRIPT.md"):
        p = os.path.join(workdir, n)
        if os.path.exists(p):
            return p
    for f in sorted(os.listdir(workdir)):
        if not f.lower().endswith(".md") or f.startswith("."):
            continue
        p = os.path.join(workdir, f)
        try:
            with open(p, encoding="utf-8") as fh:
                head = fh.read(6000)
        except Exception:
            continue
        if "文案：" in head or "文案:" in head:
            return p
    raise FileNotFoundError(
        "工作目录里没有找到脚本。请新建 script.md，用「# 分镜 / 文案：...」的格式写。")


# ---------------------------------------------------------------- 取素材
class ClipPool(object):
    """未指定素材时的自动轮播池：工作目录下所有视频按路径排序取用。"""

    def __init__(self, workdir):
        self.files = walk_files(workdir, VIDEO_EXT)
        self.turn = 0

    def pick(self):
        if not self.files:
            raise RuntimeError(
                "工作目录里没有任何视频素材。请把片段放进来，"
                "或在分镜里用「图片：文件名」配图。")
        p = self.files[self.turn % len(self.files)]
        self.turn += 1
        return p


def fill_slot(draft, clip_path, t_start, slot_us, w, h, fps, cursor_store, key):
    """把一个分镜的时长用同一素材的连续区段填满。"""
    norm = prep.normalize(clip_path, w, h, fps)
    total_us = int(prep.clip_duration(norm) * US)
    if total_us <= 0:
        raise RuntimeError("素材时长异常: %s" % clip_path)
    cursor = cursor_store.get(key, 0)
    filled = 0
    while filled < slot_us - 1000:
        remain = total_us - cursor
        if remain < int(0.2 * US):
            cursor, remain = 0, total_us
        take = min(remain, slot_us - filled)
        draft.add_video(norm, t_start + filled, take, source_start_us=cursor)
        cursor += take
        filled += take
    cursor_store[key] = cursor


# ---------------------------------------------------------------- 剪映配音
def _cut_voices(draft_root, draft_name, build_dir, plan):
    """剪映朗读模式：把同名草稿里的朗读音频收进本地库，再按字幕取时长。

    返回 (voices, stat)：
        voices  [[(wav, 秒, []), None, ...], ...]，与 plan 同形状；
                单条为 None 表示这条没配音（新写的 / 还没朗读），后续按字数估。
        stat    {"hit","total","lib","harvest"}
    一条都没命中时 voices 返回 None，让调用方走纯估算流程。
    """
    stat = {"hit": 0, "total": sum(len(c) for c in plan), "lib": 0, "harvest": None}

    if not draft_root:
        try:
            draft_root = jylib.find_draft_root()
        except Exception:
            draft_root = None
    draft_dir = os.path.join(draft_root, draft_name) if draft_root else None
    if draft_dir and os.path.isdir(draft_dir):
        try:
            _lib, h = cuttts.harvest(draft_dir, build_dir)
            stat["harvest"] = h
            if h["found"]:
                print("剪映配音: 从草稿「%s」收回 %d 条朗读音频（新复制 %d）"
                      % (draft_name, h["found"], h["copied"]))
        except Exception as e:      # 草稿只读，收不回来不该阻塞出片
            print("! 读取草稿里的朗读音频失败：%s" % e)

    lib = cuttts.load(build_dir)
    stat["lib"] = len(lib)
    if not lib:
        return None, stat

    flat = [c for chunks in plan for c in chunks]
    hit = sum(1 for t in flat if t in lib)
    stat["hit"] = hit
    if not hit:
        return None, stat

    rows = []
    for chunks in plan:
        cur = []
        for t in chunks:
            if t in lib:
                wav, dur = lib[t]
                cur.append((wav, dur, []))
            else:
                cur.append(None)
        rows.append(cur)
    return rows, stat


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(
        description="把「素材目录 + 文案」排成剪映草稿",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir", nargs="?", default=".",
                    help="工作目录（放素材和 script.md 的地方），默认当前目录")
    ap.add_argument("--script", default=None, help="指定脚本文件（相对工作目录）")
    ap.add_argument("--name", default=None, help="草稿名，默认用脚本里的「标题」")
    ap.add_argument("--draft-root", default=None, help="剪映草稿根目录（一般不用填）")
    ap.add_argument("--dry-run", action="store_true", help="只打印分镜表，不写任何文件")
    ap.add_argument("--no-cache", action="store_true", help="忽略缓存，强制重新转码/配音")
    ap.add_argument("--overwrite", action="store_true",
                    help="允许覆盖同名草稿（会先备份整个旧草稿；不加则拒绝）")
    ap.add_argument("--debug", action="store_true", help="出错时打印完整堆栈")
    args = ap.parse_args()

    workdir = os.path.abspath(args.workdir)
    if not os.path.isdir(workdir):
        sys.exit("工作目录不存在: %s" % workdir)

    build_dir = os.path.join(workdir, BUILD_DIR)
    prep.set_workdir(build_dir)
    if args.no_cache:
        prep.CACHE_VERSION = "nocache-" + str(os.getpid())

    script = find_script(workdir, args.script)
    cfg, shots = parse_script(script)
    if not shots:
        sys.exit("脚本里没有分镜（用 # 开头分行，每行写「文案：...」）")

    try:
        w, h = [int(x) for x in cfg["size"].lower().split("x")]
    except Exception:
        w, h = 1920, 1080
    fps = float(cfg["fps"])
    gap_us = int(float(cfg["gap"]) * US)
    sub_y = float(cfg["subtitle_y"])
    use_tts = cfg["tts"].lower() == "edge"

    draft_name = args.name or cfg["title"] or os.path.basename(workdir)

    print("工作目录: %s" % workdir)
    print("脚本:     %s" % rel(script, workdir))
    print("草稿名:   %s" % draft_name)

    if use_tts:
        import tts
        if not tts.available():
            print("! edge-tts 不可用（.vendor 缺失），回退到「剪映」模式：按字数估算时长")
            use_tts = False
        else:
            tts.set_workdir(build_dir)
            if args.no_cache:
                tts.set_no_cache(True)

    # ---- 1. 切字幕 + 批量合成配音 ----
    plan = []
    for shot in shots:
        chunks = [] if shot["不字幕"] or not shot["文案"].strip() \
            else split_subtitle(shot["文案"])
        plan.append(chunks)

    voices = None
    cut_stat = None
    if use_tts:
        flat = [c for chunks in plan for c in chunks]
        if flat:
            print("合成配音: %d 条字幕，音色 %s，语速 %s"
                  % (len(flat), cfg["voice"], cfg["tts_rate"]))
            res = tts.synthesize(flat, voice=cfg["voice"], rate=cfg["tts_rate"])
            it = iter(res)
            voices = [[next(it) for _ in chunks] for chunks in plan]
        else:
            voices = [[] for _ in plan]
    else:
        # 剪映朗读模式：先试着从同名草稿里把用户已经朗读好的音频收进本地库。
        # 命中越多，排出来越贴真实语速；一条都没命中就退回归字数估算。
        voices, cut_stat = _cut_voices(args.draft_root, draft_name, build_dir, plan)

    # ---- 2. 排分镜（这一步只算，不落盘）----
    pool = ClipPool(workdir)
    rows = []
    t = 0
    for i, shot in enumerate(shots, 1):
        chunks = plan[i - 1]

        segs = []
        if voices is not None and chunks:
            for text, v in zip(chunks, voices[i - 1]):
                if v is not None:
                    wav, dur, words = v
                    segs.append((text, int(round(dur * US)), wav, words))
                else:
                    # 这条没有配音（新写的、或还没在剪映里朗读）：单独按估速算，
                    # 不按整段比例分摊，否则一条缺配音会把同分镜其他条一起带偏
                    est = clean_len(text) / float(cfg["est_rate"]) * US
                    segs.append((text, max(int(est), int(0.6 * US)), None, []))
            dur_us = sum(s[1] for s in segs)
        else:
            total = int(clean_len(shot["文案"]) / float(cfg["est_rate"]) * US) \
                if shot["文案"].strip() else int(2 * US)
            dur_us = total
            if chunks:
                tc = sum(clean_len(c) for c in chunks)
                for c in chunks:
                    segs.append((c, int(total * clean_len(c) / tc), None, []))

        if shot["时长"]:
            dur_us = int(shot["时长"] * US)
        slot_us = dur_us + gap_us

        if shot["图片"]:
            src = resolve_asset(shot["图片"], workdir, IMAGE_EXT)
            kind = "image"
        elif shot["素材"]:
            src = resolve_asset(shot["素材"], workdir, VIDEO_EXT)
            kind = "video"
        else:
            src = pool.pick()
            kind = "video"

        rows.append({"i": i, "start": t, "slot": slot_us, "src": src,
                     "kind": kind, "segs": segs, "text": shot["文案"],
                     "y": shot["位置Y"], "fs": shot["字号"]})
        t += slot_us

    total_us = t

    # ---- 3. 分镜表 ----
    print("\n%-4s %-8s %-7s %-26s %s" % ("#", "起始", "时长", "素材", "文案"))
    print("-" * 92)
    for r in rows:
        rpath = rel(r["src"], workdir)
        tag = ("图片 " if r["kind"] == "image" else "") + rpath
        print("%-4d %-8.2f %-7.2f %-26s %s"
              % (r["i"], r["start"] / US, r["slot"] / US, tag[:24], r["text"][:30]))
    print("-" * 92)
    if use_tts:
        voice_desc = "edge " + cfg["voice"]
    elif cut_stat and cut_stat["hit"]:
        voice_desc = "剪映配音 %d/%d 条" % (cut_stat["hit"], cut_stat["total"])
    else:
        voice_desc = "剪映朗读（按字数估算）"
    print("总时长 %.2f 秒 / %d 分镜 / %d 字幕 / %dx%d@%gfps / 配音=%s"
          % (total_us / US, len(rows), sum(len(r["segs"]) for r in rows), w, h, fps,
             voice_desc))
    if cut_stat and 0 < cut_stat["hit"] < cut_stat["total"]:
        print("! %d 条字幕在配音库里没有对应音频，已按 %.1f 字/秒估算"
              % (cut_stat["total"] - cut_stat["hit"], float(cfg["est_rate"])))

    all_segs = [s for r in rows for s in r["segs"]]
    if use_tts and all_segs:
        with_words = sum(1 for s in all_segs if s[3])
        note = "" if with_words == len(all_segs) else "（缺的是旧缓存，加 --no-cache 可补齐）"
        print("词级时间 %d/%d 条%s" % (with_words, len(all_segs), note))

    if args.dry_run:
        print("\n[dry-run] 未生成草稿。转码/配音缓存仍会落在 %s（下次生成可直接复用）"
              % BUILD_DIR)
        return

    # ---- 4. 生成草稿 ----
    os.makedirs(build_dir, exist_ok=True)
    draft = Draft(draft_name, width=w, height=h, fps=fps, root=args.draft_root)
    cursor_store = {}
    for r in rows:
        if r["kind"] == "image":
            still = prep.still(r["src"], r["slot"] / US, w, h, fps)
            draft.add_video(still, r["start"], r["slot"])
        else:
            fill_slot(draft, r["src"], r["start"], r["slot"], w, h, fps,
                      cursor_store, r["src"])

        acc = 0
        for text, seg_us, wav, _words in r["segs"]:
            draft.add_text(text, r["start"] + acc, seg_us,
                           y=r["y"] if r["y"] is not None else sub_y,
                           font_size=r["fs"] or float(cfg["font_size"]),
                           color=cfg["color"], stroke_color=cfg["stroke"])
            if wav:
                draft.add_audio(wav, r["start"] + acc, seg_us)
            acc += seg_us

    path = draft.save(overwrite=args.overwrite, backup_dir=workdir)

    # ---- 5. 留一份可移植的副本供 check/追溯 ----
    backup = os.path.join(build_dir, "draft_content.json")
    with open(backup, "w", encoding="utf-8") as f:
        json.dump(draft.d, f, ensure_ascii=False)

    # 字幕时间轴：绝对毫秒，词级偏移已叠加到整条时间线上。
    # 逐字高亮、SRT 导出都从这里取，别再自己算一遍。
    timeline = {"draft": draft_name, "width": w, "height": h, "fps": fps,
                "duration": round(total_us / US, 3),
                "voice": cfg["voice"] if use_tts else ("剪映" if voices is not None else None),
                "chunks": []}
    for r in rows:
        acc = 0
        for text, seg_us, wav, words in r["segs"]:
            base = (r["start"] + acc) / US
            timeline["chunks"].append({
                "scene": r["i"],
                "text": text,
                "start": round(base, 3),
                "duration": round(seg_us / US, 3),
                "audio": rel(wav, workdir).replace("\\", "/") if wav else None,
                "words": [{"text": x["text"],
                           "start": round(base * 1000 + x["start"], 1),
                           "end": round(base * 1000 + x["end"], 1)}
                          for x in words],
            })
            acc += seg_us
    with open(os.path.join(build_dir, "字幕时间轴.json"), "w", encoding="utf-8") as f:
        json.dump(timeline, f, ensure_ascii=False, indent=1)

    with open(os.path.join(build_dir, "分镜表.md"), "w", encoding="utf-8") as f:
        f.write("# %s\n\n总时长 %.2f 秒 · %d 分镜 · %d 字幕 · %dx%d@%gfps\n\n"
                % (draft_name, total_us / US, len(rows),
                   sum(len(r["segs"]) for r in rows), w, h, fps))
        f.write("| # | 起始 | 时长 | 素材 | 字幕数 | 文案 |\n|---|---|---|---|---|---|\n")
        for r in rows:
            f.write("| %d | %.2f | %.2f | %s | %d | %s |\n"
                    % (r["i"], r["start"] / US, r["slot"] / US,
                       rel(r["src"], workdir).replace("|", "\\|"),
                       len(r["segs"]), r["text"].replace("|", "\\|")))
        f.write("\n## 字幕明细\n\n| 开始 | 时长 | 文本 | 音频 |\n|---|---|---|---|\n")
        for r in rows:
            acc = 0
            for text, seg_us, wav, _words in r["segs"]:
                f.write("| %.2f | %.2f | %s | %s |\n"
                        % ((r["start"] + acc) / US, seg_us / US,
                           text.replace("|", "\\|"),
                           "有" if wav else "无"))
                acc += seg_us

    print("\n草稿已生成: %s" % path)
    print("中间产物:   %s" % build_dir)
    if use_tts:
        print("配音已写入草稿，剪映里直接预览即可")
    elif cut_stat and cut_stat["hit"] == cut_stat["total"]:
        print("用剪映配音排好了，剪映里直接预览即可（音频已在本地库里）")
    elif cut_stat and cut_stat["hit"]:
        print("部分字幕还没配音。补齐：在剪映里只朗读缺的那几条 → 关掉剪映 → 重跑本命令")
    else:
        print("下一步：选中字幕轨 → 文本朗读 → 生成配音（语速建议 1.0）")
        print("朗读完关掉剪映，重跑本命令即可按真实配音时长重排")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        if "--debug" in sys.argv:
            raise
        sys.exit("错误: %s\n（加 --debug 查看完整堆栈）" % e)
