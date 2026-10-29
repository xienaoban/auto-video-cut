# -*- coding: utf-8 -*-
"""
从零造一份剪映草稿（剪映还没打开过的那种）。

纯 Python 标准库 + 系统已装的 ffmpeg/ffprobe，不需要安装任何第三方包。

和 jycrypto / jycore / jyedit 的分工：
    这里写的是**全新的明文草稿**，剪映第一次打开时会自己做容错、补出 Timelines 目录。
    草稿一旦被剪映接管（加密 + 多时间轴），增量修改请走 jyedit.Editor，
    不要用这个类去覆盖——那会把剪映写进去的东西抹掉。
"""
import json
import os
import shutil
import subprocess
import time
import uuid

import jycore

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
CONFIG = os.path.join(HERE, "config.json")

US = 1_000_000  # 剪映内部时间单位：微秒


def find_draft_root(explicit=None):
    """定位剪映草稿根目录。本工具可整体搬走，所以不写死路径。

    优先级：显式参数 > 环境变量 JIANYING_DRAFT_ROOT > config.json 的 draft_root
            > 常见安装位置自动探测。
    """
    if explicit:
        return os.path.normpath(os.path.expandvars(os.path.expanduser(explicit)))

    cands = []
    env = os.environ.get("JIANYING_DRAFT_ROOT")
    if env:
        cands.append(env)
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                p = (json.load(f) or {}).get("draft_root")
            if p and not p.startswith("<"):
                cands.append(os.path.expandvars(os.path.expanduser(p)))
        except Exception:
            pass

    bases = [os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA"),
             os.path.expanduser("~/AppData/Local")]
    for base in bases:
        if not base:
            continue
        for app in ("JianyingPro", "JianyingPro Beta", "CapCut"):
            cands.append(os.path.join(base, app, "User Data", "Projects",
                                      "com.lveditor.draft"))

    # 有索引文件的优先（说明剪映确实在这里建过草稿）
    for c in cands:
        if c and os.path.exists(os.path.join(c, "root_meta_info.json")):
            return os.path.normpath(c)
    for c in cands:
        if c and os.path.isdir(c):
            return os.path.normpath(c)
    return None


DRAFT_ROOT_HINT = (
    "找不到剪映草稿目录。请在本工具目录下建 config.json（参考 config.example.json）写\n"
    '  {"draft_root": "%LOCALAPPDATA%/JianyingPro/User Data/Projects/com.lveditor.draft"}\n'
    "（%LOCALAPPDATA% 会自动展开，也可直接写字面路径）\n"
    "或设置环境变量 JIANYING_DRAFT_ROOT，或用参数 --draft-root 指定。")


# 模板字体来自剪映素材库缓存，换机器会失效；失效时退回系统字体
FONT_FALLBACKS = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


_cfg_cache = None


def load_config():
    """读工具目录下的 config.json（不入库，放本机相关的配置）。"""
    global _cfg_cache
    if _cfg_cache is None:
        try:
            with open(CONFIG, encoding="utf-8") as f:
                _cfg_cache = json.load(f) or {}
        except Exception:
            _cfg_cache = {}
    return _cfg_cache


def resolve_font(font_path, font_resource_id):
    """返回 (path, resource_id)。优先用 config.json 里本机指定的字体，
    其次是模板自带路径，都不存在则退回系统字体并解除素材库绑定。"""
    cfg_font = str(load_config().get("font_path") or "")
    if cfg_font:
        cfg_font = os.path.expandvars(os.path.expanduser(cfg_font))
        if os.path.exists(cfg_font):
            return cfg_font, ""
    if font_path and os.path.exists(font_path):
        return font_path, font_resource_id
    for c in FONT_FALLBACKS:
        if os.path.exists(c):
            return c, ""
    return font_path or "", font_resource_id


def uid():
    return str(uuid.uuid4()).upper()


def _load(name):
    with open(os.path.join(ASSETS, name), encoding="utf-8") as f:
        return json.load(f)


def _clone(obj):
    return json.loads(json.dumps(obj))


def hex_to_rgb(h):
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]


def ffprobe(path):
    """返回 dict: duration(秒), width, height, has_audio"""
    cmd = [jycore.tool("ffprobe"), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError("ffprobe 失败: %s\n%s" % (path, out.stderr[:400]))
    info = json.loads(out.stdout)
    w = h = 0
    has_audio = False
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and s.get("width"):
            w, h = int(s["width"]), int(s["height"])
        if s.get("codec_type") == "audio":
            has_audio = True
    dur = float(info.get("format", {}).get("duration") or 0)
    return {"duration": dur, "width": w, "height": h, "has_audio": has_audio}


class Draft(object):
    def __init__(self, name, width=1920, height=1080, fps=30,
                 root=None, draft_type=""):
        self.name = name
        self.width = width
        self.height = height
        self.fps = float(fps)
        self.root = root or find_draft_root()
        self.draft_id = uid()

        self.d = _load("skeleton.json")
        self._vtpl = _load("video_material.json")
        self._ttpl = _load("text_material.json")
        self._atpl = _load("audio_material.json")
        self._vseg = _load("video_segment.json")
        self._tseg = _load("text_segment.json")
        self._aseg = _load("audio_segment.json")

        self.d["id"] = self.draft_id
        self.d["name"] = name
        self.d["draft_type"] = draft_type
        self.d["fps"] = self.fps
        self.d["canvas_config"] = {
            "background": None, "height": height,
            "ratio": "original", "width": width,
        }

        self.video_segs = []
        self.text_segs = []
        self.audio_segs = []
        self._end = 0

    # ---------------- 视频 / 图片（须已由 prep 规整到画布尺寸）----------------
    def add_video(self, path, start_us, duration_us, source_start_us=0,
                  fit_scale=1.0, x=0.0, y=0.0, opacity=1.0, label=None):
        meta = ffprobe(path)
        mat = _clone(self._vtpl)
        mat.update({
            "id": uid(),
            "material_name": os.path.basename(path),
            "path": path,
            "media_path": path,
            "duration": int(meta["duration"] * US),
            "width": meta["width"] or self.width,
            "height": meta["height"] or self.height,
            "has_audio": bool(meta["has_audio"]),
        })
        if label:
            mat["material_name"] = label

        seg = _clone(self._vseg)
        seg.update({
            "id": uid(),
            "material_id": mat["id"],
            "target_timerange": {"start": int(start_us), "duration": int(duration_us)},
            "source_timerange": {"start": int(source_start_us), "duration": int(duration_us)},
            "extra_material_refs": [],
            "speed": 1.0,
            "visible": True,
        })
        seg["clip"] = {
            "alpha": opacity,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0,
            "scale": {"x": fit_scale, "y": fit_scale},
            "transform": {"x": x, "y": y},
        }
        seg["uniform_scale"] = {"on": True, "value": fit_scale}

        self.d["materials"]["videos"].append(mat)
        self.video_segs.append(seg)
        return seg

    # ---------------- 字幕 / 文字 ----------------
    def add_text(self, text, start_us, duration_us, y=-0.78, font_size=15,
                 color="#ffffff", stroke_color="#000000", stroke_width=0.08,
                 alignment=1, opacity=1.0, scale=1.0, letter_spacing=0):
        mat = _clone(self._ttpl)
        tpl_font = mat.get("font_path", "")
        font_path, font_rid = resolve_font(tpl_font,
                                           mat.get("font_resource_id", ""))
        if font_path != tpl_font:
            # 模板自带字体（别人机器上的剪映缓存路径）本机没有。
            # 同步 fonts 素材数组，避免草稿里残留别人机器上的绝对路径。
            using_system = font_path in FONT_FALLBACKS
            for f in mat.get("fonts") or []:
                f["path"] = font_path
                if using_system:
                    # 系统字体不属于剪映素材库，解绑，否则剪映会去拉原字体
                    f["resource_id"] = ""
                    f["effect_id"] = ""
                    f["title"] = "系统字体"
                    f["category_id"] = ""
                    f["category_name"] = ""
                    f["source_platform"] = 0
        content = {
            "text": text,
            "styles": [{
                "fill": {"content": {"render_type": "solid",
                                     "solid": {"color": hex_to_rgb(color)}}},
                "font": {"path": font_path, "id": font_rid},
                "strokes": [{
                    "content": {"render_type": "solid",
                                "solid": {"color": hex_to_rgb(stroke_color)}},
                    "width": stroke_width, "mode": 0,
                }],
                "size": font_size,
                "useLetterColor": True,
                "letter_spacing": letter_spacing,
                "range": [0, len(text)],
            }],
        }
        mat.update({
            "id": uid(),
            "content": json.dumps(content, ensure_ascii=False),
            "font_path": font_path,
            "font_resource_id": font_rid,
            "font_size": font_size,
            "text_size": max(1, int(round(font_size / 15.0 * 30))),
            "text_color": color,
            "border_color": stroke_color,
            "border_width": stroke_width,
            "alignment": alignment,
            "letter_spacing": letter_spacing,
            "use_effect_default_color": True,
            "line_spacing": 0.02,
            "line_max_width": 0.82,
            "background_color": "#000000",
        })

        seg = _clone(self._tseg)
        seg.update({
            "id": uid(),
            "material_id": mat["id"],
            "target_timerange": {"start": int(start_us), "duration": int(duration_us)},
            "extra_material_refs": [],
        })
        seg.pop("source_timerange", None)
        seg["clip"] = {
            "alpha": opacity,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0,
            "scale": {"x": scale, "y": scale},
            "transform": {"x": 0.0, "y": y},
        }

        self.d["materials"]["texts"].append(mat)
        self.text_segs.append(seg)
        return seg

    # ---------------- 音频（可选，用于自录音或成品配音）----------------
    def add_audio(self, path, start_us, duration_us=None, volume=1.0):
        meta = ffprobe(path)
        real = int(meta["duration"] * US)
        dur = int(duration_us if duration_us else real)
        # 夹一下：剪映朗读音频的声明时长是帧对齐向上取整的，比 wav 实际长 0~1 帧。
        # 不夹的话 source_timerange 会越出素材末尾（validate 报「源区间越界」）。
        dur = min(dur, real)
        mat = _clone(self._atpl)
        mat.update({
            "id": uid(),
            "path": path,
            "name": os.path.basename(path),
            "duration": real,
        })
        seg = _clone(self._aseg)
        seg.update({
            "id": uid(),
            "material_id": mat["id"],
            "target_timerange": {"start": int(start_us), "duration": dur},
            "source_timerange": {"start": 0, "duration": dur},
            "extra_material_refs": [],
            "volume": volume,
        })
        self.d["materials"]["audios"].append(mat)
        self.audio_segs.append(seg)
        return seg

    # ---------------- 组装与落盘 ----------------
    def _build_tracks(self):
        tracks = []
        if self.video_segs:
            tracks.append({"attribute": 0, "flag": 0, "id": uid(),
                           "is_default_name": True, "name": "",
                           "segments": self.video_segs, "type": "video"})
        if self.text_segs:
            tracks.append({"attribute": 0, "flag": 0, "id": uid(),
                           "is_default_name": True, "name": "",
                           "segments": self.text_segs, "type": "text"})
        if self.audio_segs:
            tracks.append({"attribute": 0, "flag": 0, "id": uid(),
                           "is_default_name": True, "name": "",
                           "segments": self.audio_segs, "type": "audio"})
        self.d["tracks"] = tracks

        end = 0
        for segs in (self.video_segs, self.text_segs, self.audio_segs):
            for s in segs:
                tr = s["target_timerange"]
                end = max(end, tr["start"] + tr["duration"])
        self.d["duration"] = end
        return end

    def save(self):
        if not self.root:
            raise RuntimeError(DRAFT_ROOT_HINT)
        end = self._build_tracks()
        now_us = int(time.time() * US)
        self.d["create_time"] = now_us
        self.d["update_time"] = now_us
        self.d["path"] = os.path.join(self.root, self.name)

        out = os.path.join(self.root, self.name)
        if os.path.exists(out):
            # 只清掉自己的文件；剪映自己生成的子目录（Timelines/textReading/.backup）
            # 可能被占用，删不掉也无所谓，属于剪映的缓存
            shutil.rmtree(out, ignore_errors=True)
        os.makedirs(out, exist_ok=True)

        stale = os.path.join(out, "draft_content.json")
        if os.path.exists(stale):
            try:
                os.remove(stale)
            except OSError:
                raise RuntimeError(
                    "草稿目录被占用，无法覆盖：%s\n"
                    "请先**完全退出剪映**（含托盘进程 JianyingProTray.exe）再重新运行。" % out)

        with open(os.path.join(out, "draft_content.json"), "w", encoding="utf-8") as f:
            json.dump(self.d, f, ensure_ascii=False)

        meta = {
            "draft_cloud_last_action_download": False,
            "draft_cloud_purchase_info": "",
            "draft_cloud_template_id": "",
            "draft_cloud_tutorial_info": "",
            "draft_cloud_videocut_purchase_info": "",
            "draft_cover": "",
            "draft_fold_path": out,
            "draft_id": self.draft_id,
            "draft_is_ai_shorts": False,
            "draft_is_invisible": False,
            "draft_json_file": os.path.join(out, "draft_content.json"),
            "draft_name": self.name,
            "draft_new_version": "",
            "draft_root_path": self.root,
            "draft_timeline_materials_size_": 0,
            "draft_type": self.d.get("draft_type", ""),
            "tm_draft_create": now_us,
            "tm_draft_modified": now_us,
            "tm_draft_removed": 0,
            "tm_duration": end,
        }
        with open(os.path.join(out, "draft_meta_info.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

        with open(os.path.join(out, "draft_settings"), "w", encoding="utf-8") as f:
            f.write("[General]\ndraft_create_time=%d\ndraft_last_edit_time=%d\n"
                    "real_edit_seconds=0\nreal_edit_keys=0\n"
                    % (int(time.time()), int(time.time())))

        self._register(meta)
        return out

    def _register(self, meta):
        idx_path = os.path.join(self.root, "root_meta_info.json")
        if os.path.exists(idx_path):
            try:
                shutil.copy2(idx_path, idx_path + ".bak")
            except OSError:
                pass
            with open(idx_path, encoding="utf-8") as f:
                idx = json.load(f)
        else:
            idx = {"all_draft_store": [], "draft_ids": [], "root_path": self.root}

        idx["all_draft_store"] = [e for e in idx.get("all_draft_store", [])
                                  if e.get("draft_fold_path") != meta["draft_fold_path"]]
        entry = dict(meta)
        entry.update({
            "cloud_draft_cover": False, "cloud_draft_sync": False,
            "draft_is_cloud_temp_draft": False, "draft_is_infinite_canvas_draft": False,
            "draft_is_pippit_draft": False, "draft_is_web_article_video": False,
            "draft_web_article_video_enter_from": "", "pippit_avatar_url": "",
            "pippit_extra_info": "", "pippit_id": "", "pippit_user_name": "",
            "streaming_edit_draft_ready": True,
        })
        idx["all_draft_store"].append(entry)
        ids = idx.setdefault("draft_ids", [])
        if isinstance(ids, list) and self.draft_id not in ids:
            ids.append(self.draft_id)
        try:
            with open(idx_path, "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False)
        except OSError:
            raise RuntimeError(
                "草稿索引写不进去，剪映大概率还在运行。\n"
                "请完全退出剪映（含托盘 JianyingProTray.exe）后重新运行。")
