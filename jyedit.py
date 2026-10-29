# -*- coding: utf-8 -*-
"""在「已经有内容的草稿」上做增删改。

和 jylib.py 的分工：
    jylib.Draft   —— 从零造一份草稿（剪映还没碰过，靠模板拼字段）
    jyedit.Editor —— 剪映已经接管过的草稿（加密、多时间轴），在它上面继续加工

原则：**不手拼字段**。新增的段落和素材一律照抄剪映自己写过的结构，
只换 id、路径、时间、文字。这样剪映自己的校验怎么严都不会踩空。

结构从哪抄，两处来源，按先后：
    1. 草稿里已经有的同类片段（尊重你在剪映里调好的样式）
    2. `assets/jy_template.json`（从 11.6 真实草稿固化的模板）

第 2 条是为了对付“你粗剪完只有视频轨”的情形——那时候草稿里一条字幕、
一段音频都没有，光靠第 1 条会直接卡住。剪映版本已锚定，所以模板不会过期。

用法：
    ed = Editor("10月30日")
    ed.open()
    ed.add_text("第一句字幕", 1_000_000, 2_500_000)
    ed.add_audio(r"E:/.../配音.mp3", 1_000_000)
    ed.add_image(r"E:/.../配图.png", 5_000_000, 3_000_000)
    ed.commit()          # 备份 + 加密写回顶层和时间轴
"""
import copy
import json
import os
import re
import subprocess
import uuid

import jycore

US = 1_000_000

TEMPLATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "jy_template.json")
_tpl_cache = {}
_app_dir_cache = []

# 模板里存着提取时那台机器的剪映安装路径（字体在里面），换机要跟着改。
# 必须限定“整串就是一条绝对路径”，否则会把 content 这种 JSON 字符串也当成路径。
APP_PATH_RE = re.compile(
    r"^(?:[A-Za-z]:|/)[^\"{}]*?(?:JianyingPro[/\\]Apps[/\\][^/\\]+)([/\\].*)$")


def fix_app_path(path):
    """把模板里写死的剪映安装目录换成当前这台机器的。

    字体是 `<剪映安装目录>/Resources/Font/SystemFont/zh-hans.ttf`，
    安装位置变了就会指到不存在的地方。
    """
    m = APP_PATH_RE.match(path)
    if not m:
        return path
    if not _app_dir_cache:
        _app_dir_cache.append(jycore.app_dir().replace("\\", "/"))
    return _app_dir_cache[0] + m.group(1)


def builtin_template(kind):
    """读取固化的片段模板。字段原样取自剪映自己保存的草稿，不做任何加工。"""
    if not _tpl_cache:
        if not os.path.exists(TEMPLATE_FILE):
            raise RuntimeError("缺少内置模板：%s" % TEMPLATE_FILE)
        with open(TEMPLATE_FILE, encoding="utf-8") as f:
            _tpl_cache.update(json.load(f))
    if kind not in _tpl_cache:
        raise RuntimeError("内置模板里没有 %s 这类片段" % kind)
    return _tpl_cache[kind]


def uid():
    return str(uuid.uuid4()).upper()


def probe(path):
    """ffprobe 拿时长和画面尺寸。图片没有 duration，返回 0。"""
    out = subprocess.run(
        [jycore.tool("ffprobe"), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError("ffprobe 读不了：%s\n%s" % (path, out.stderr[:300]))
    info = json.loads(out.stdout or "{}")
    w = h = 0
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and s.get("width"):
            w, h = int(s["width"]), int(s["height"])
    return {"duration": float(info.get("format", {}).get("duration") or 0),
            "width": w, "height": h}


class Editor(object):
    def __init__(self, name, root=None, work_dir=None, media_root=None):
        self.name = name
        self.dir = jycore.draft_dir(name, root)
        # 本次视频工程目录：默认从 AVC_PROJECT_DIR 读，读不到就报错。
        # 显式传 media_root 才能绕过（隔离测试用；传 "" 表示不搬动素材）。
        self.media_root = jycore.project_dir() if media_root is None else media_root
        self.work_dir = work_dir or self.media_root or os.getcwd()
        self.doc = None
        self._snapshot = None

    # ------------------------------------------------------------ 读
    def open(self):
        self.doc = jycore.read(jycore.content_path(self.dir))
        self._snapshot = {
            "tracks": [(t.get("type"), len(t.get("segments") or []))
                       for t in self.doc["tracks"]],
            "seg_ids": [s["id"] for t in self.doc["tracks"]
                        for s in t.get("segments") or []],
            "duration": self.doc["duration"],
        }
        return self

    def _intake(self, path, kind, filename=None):
        """把素材收进工程目录。media_root 传 "" 时原样返回（隔离测试用）。"""
        if self.media_root == "":
            return jycore.slash(path)
        return jycore.import_media(path, self.media_root, kind, filename)

    @property
    def mats(self):
        return self.doc["materials"]

    def summary(self):
        return " / ".join("%s×%d" % (t, n) for t, n in self._snapshot["tracks"])

    # ------------------------------------------------------------ 克隆工具
    def _first_segment(self, track_type):
        for t in self.doc["tracks"]:
            if t.get("type") == track_type and t.get("segments"):
                return t["segments"][0]
        return None

    def _material_of(self, seg, kind):
        for m in self.mats.get(kind, []):
            if m.get("id") == seg["material_id"]:
                return m
        raise RuntimeError("片段引用的素材不在 %s 里：%s" % (kind, seg["material_id"]))

    def _clone_refs(self, seg):
        """附属素材（速度/占位/节拍/声道/人声分离/动画）复制一份并换 id。

        每个段落独占自己的一套附属素材，共享会互相串改。注意：文字片段其实
        不挂 `extra_material_refs` 也能正常显示（已实测），这里照抄剪映自己的
        写法，是为了少一个变量，不是因为缺了会被丢弃。
        """
        new_refs = []
        for ref in seg.get("extra_material_refs") or []:
            for arr in self.mats.values():
                hit = next((m for m in arr if m.get("id") == ref), None)
                if hit is not None:
                    nm = copy.deepcopy(hit)
                    nm["id"] = uid()
                    arr.append(nm)
                    new_refs.append(nm["id"])
                    break
        return new_refs

    def _from_builtin(self, kind):
        """用内置模板造一套新片段 + 素材，全部 id 换新。

        模板里的素材互相之间有 id 引用（片段的 material_id、extra_material_refs，
        附属素材之间的 material_id 等），所以不是逐个换，而是先把所有旧 id 建成
        一张映射表，再递归遍历整棵结构替换。这样漏改的概率是零。
        """
        tpl = builtin_template(kind)
        seg = copy.deepcopy(tpl["segment"])
        mats = copy.deepcopy(tpl["materials"])

        olds = [seg["id"]]
        for arr in mats.values():
            olds.extend(m["id"] for m in arr)
        remap = {old: uid() for old in olds}

        def fix(node):
            if isinstance(node, dict):
                return {k: fix(v) for k, v in node.items()}
            if isinstance(node, list):
                return [fix(v) for v in node]
            if isinstance(node, str):
                return fix_app_path(remap.get(node, node))
            return node

        seg = fix(seg)
        for k, arr in mats.items():
            self.mats.setdefault(k, [])
            self.mats[k].extend(fix(arr))
        return seg

    def _append_track(self, track_type, seg):
        seg["track_render_index"] = len(self.doc["tracks"])
        self.doc["tracks"].append({
            "id": uid(), "type": track_type, "is_default_name": True,
            "segments": [seg],
        })

    # ------------------------------------------------------------ 文字
    def add_text(self, text, start_us, duration_us, template=None):
        """加一条字幕。

        template 决定样式从哪来：
            None（默认）  草稿里已有文字片段就沿用它——尊重你在剪映里调好的字体
                         和描边；一条都没有（刚粗剪完就是这样）就退回内置模板
            "builtin"     强制用内置模板，不受草稿里已有字幕影响
            其他字符串    以文本内容等于它的那条字幕为样式模板
        """
        tpl_seg = None
        if template != "builtin":
            found = False
            for t in self.doc["tracks"]:
                if t.get("type") != "text":
                    continue
                for s in t.get("segments") or []:
                    if template is None:
                        tpl_seg = s
                        break
                    m = self._material_of(s, "texts")
                    if json.loads(m["content"]).get("text") == template:
                        tpl_seg = s
                        break
                if tpl_seg:
                    found = True
                    break
            if template not in (None, "builtin") and not found:
                raise RuntimeError("草稿里没有文本为「%s」的字幕可作模板。" % template)

        if tpl_seg is None:
            seg = self._from_builtin("text")
            mat = self._material_of(seg, "texts")
        else:
            mat = copy.deepcopy(self._material_of(tpl_seg, "texts"))
            mat["id"] = uid()
            self.mats["texts"].append(mat)
            seg = copy.deepcopy(tpl_seg)
            seg["id"] = uid()
            seg["material_id"] = mat["id"]
            seg["extra_material_refs"] = self._clone_refs(tpl_seg)

        content = json.loads(mat["content"])
        content["text"] = text
        for st in content.get("styles") or []:
            st["range"] = [0, len(text)]
            # content 是 JSON 字符串，递归替换够不到里面，字体路径单独修
            font = st.get("font") or {}
            if font.get("path"):
                font["path"] = fix_app_path(font["path"])
        mat["content"] = json.dumps(content, ensure_ascii=False,
                                    separators=(",", ":"))
        mat.pop("text_to_audio_ids", None)   # 不继承别人的朗读关联

        seg["target_timerange"] = {"start": int(start_us),
                                   "duration": int(duration_us)}
        seg.pop("common_keyframes", None)
        self._append_track("text", seg)
        return seg

    def set_text(self, old, new):
        """把已有字幕的文本改掉，返回改了几条。"""
        n = 0
        for m in self.mats.get("texts", []):
            c = json.loads(m["content"])
            if c.get("text") == old:
                c["text"] = new
                for st in c.get("styles") or []:
                    st["range"] = [0, len(new)]
                m["content"] = json.dumps(c, ensure_ascii=False,
                                          separators=(",", ":"))
                n += 1
        return n

    # ------------------------------------------------------------ 音频
    def add_audio(self, path, start_us, duration_us=None, volume=1.0):
        """加一段配音/音乐。草稿里已有音频片段就沿用它的结构，没有就用内置模板。

        素材一律换成 extract_music 指向本地文件，不碰剪映的云端音色，
        所以导出时不会触发会员校验。
        """
        path = self._intake(path, "audio")
        meta = probe(path)
        real = int(meta["duration"] * US)
        dur = min(int(duration_us), real) if duration_us else real

        tpl_seg = self._first_segment("audio")
        if tpl_seg is None:
            seg = self._from_builtin("audio")
            mat = self._material_of(seg, "audios")
        else:
            mat = copy.deepcopy(self._material_of(tpl_seg, "audios"))
            mat["id"] = uid()
            self.mats["audios"].append(mat)
            seg = copy.deepcopy(tpl_seg)
            seg["id"] = uid()
            seg["material_id"] = mat["id"]
            seg["extra_material_refs"] = self._clone_refs(tpl_seg)

        mat.update({
            "type": "extract_music",
            "name": os.path.basename(path),
            "path": path,
            "duration": real,
            "text_id": "",
            "tone_speaker": "",
            "tone_effect_name": "",
            "tone_type": "",
            "tone_platform": "",
            "resource_id": "",
        })

        seg["target_timerange"] = {"start": int(start_us), "duration": dur}
        seg["source_timerange"] = {"start": 0, "duration": dur}
        seg["volume"] = volume
        self._append_track("audio", seg)
        return seg

    def replace_media(self, seg_id, path, name=None, filename=None):
        """把片段引用的素材换成一份本地文件，片段本身（id/时间/附属素材）不动。

        两个用途：

        1. 剪映「文本朗读」的素材 type=text_to_audio，带 resource_id /
           tone_speaker / tone_platform，指向剪映云端音色。导出时它会当 TTS
           产物处理，联网校验不过就可能没声音。换成 extract_music 指向本地
           wav，就成了普通音频，导出不依赖云端。
        2. 把指向 C 盘草稿缓存、剪映缓存、工具 tmp 的素材，换成工程目录里的副本。

        设了 media_root 时，文件会先复制进 <工程>/音频 或 <工程>/图片。
        音频时长按文件真实长度截断，避免 source_timerange 越界。
        """
        seg = None
        for t in self.doc["tracks"]:
            for s in t.get("segments") or []:
                if s["id"] == seg_id:
                    seg = s
                    break
            if seg is not None:
                break
        if seg is None:
            raise RuntimeError("草稿里没有这个片段：%s" % seg_id)

        old_id = seg["material_id"]
        kind, arr = None, None
        for key, k in (("audios", "audio"), ("videos", "image")):
            if any(m["id"] == old_id for m in self.mats.get(key, [])):
                kind, arr = k, self.mats[key]
                break
        if kind is None:
            raise RuntimeError("片段引用的素材既不在 audios 也不在 videos 里：%s"
                               % old_id)
        old = next(m for m in arr if m["id"] == old_id)

        path = self._intake(path, kind, filename)
        meta = probe(path)
        real = int(meta["duration"] * US)
        target_dur = int(seg["target_timerange"]["duration"])

        if kind == "audio":
            tpl = next((m for m in arr if m.get("type") == "extract_music"),
                       None) or old
            mat = copy.deepcopy(tpl)
            for k in ("text_id", "tone_speaker", "tone_effect_name",
                      "tone_type", "tone_platform"):
                mat.pop(k, None)
            dur = min(target_dur, real)
            seg["source_timerange"] = {"start": 0, "duration": dur}
            seg["target_timerange"] = dict(seg["target_timerange"], duration=dur)
            # 文本素材上的朗读回路一起断掉（它存的是片段 id，不是素材 id）
            for m in self.mats.get("texts", []):
                ids = m.get("text_to_audio_ids") or []
                if seg_id in ids:
                    m["text_to_audio_ids"] = [i for i in ids if i != seg_id]
        else:
            mat = copy.deepcopy(old)
            mat.update({
                "type": "photo",
                "material_name": name or os.path.basename(path),
                "has_audio": False,
                "width": meta["width"],
                "height": meta["height"],
                "duration": target_dur,
            })

        mat.update({
            "id": uid(),
            "name": name or os.path.basename(path),
            "path": path,
            "duration": min(target_dur, real) if kind == "audio" else target_dur,
            "resource_id": "",
        })
        arr.append(mat)
        seg["material_id"] = mat["id"]

        # 旧素材没人再引用就删掉，免得剪映仍认为这里挂着一次朗读
        still = [s for t in self.doc["tracks"] for s in (t.get("segments") or [])
                 if s.get("material_id") == old_id]
        if not still:
            arr.remove(old)
        return seg

    # ------------------------------------------------------------ 图片
    def add_image(self, path, start_us, duration_us, x=0.0, y=0.0,
                  scale=1.0, opacity=1.0):
        tpl_seg = self._first_segment("video")
        if tpl_seg is None:
            raise RuntimeError("草稿里没有视频片段可当模板。")
        path = self._intake(path, "image")
        meta = probe(path)
        dur = int(duration_us)

        mat = copy.deepcopy(self._material_of(tpl_seg, "videos"))
        mat.update({
            "id": uid(),
            "type": "photo",
            "material_name": os.path.basename(path),
            "path": path,
            "has_audio": False,
            "width": meta["width"],
            "height": meta["height"],
            "duration": dur,
        })
        self.mats["videos"].append(mat)

        seg = copy.deepcopy(tpl_seg)
        seg["id"] = uid()
        seg["material_id"] = mat["id"]
        seg["extra_material_refs"] = self._clone_refs(tpl_seg)
        seg["target_timerange"] = {"start": int(start_us), "duration": dur}
        seg["source_timerange"] = {"start": 0, "duration": dur}
        seg["clip"] = {
            "alpha": opacity,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0,
            "scale": {"x": scale, "y": scale},
            "transform": {"x": x, "y": y},
        }
        seg.pop("common_keyframes", None)
        self._append_track("video", seg)
        return seg

    # ------------------------------------------------------------ 关键帧
    def keyframes(self, seg, points, prop="KFTypePositionY"):
        """给片段打关键帧。points = [(相对毫秒, 值), ...]，按时间升序。

        prop 只列剪映实际会用的这几种：
            KFTypePositionX / KFTypePositionY  —— 位移，单位是画布比例
            KFTypeScaleX                       —— 缩放
            KFTypeRotation                     —— 旋转，单位度
        """
        def kf(t_off, val):
            return {"id": uid(), "time_offset": int(t_off * 1000),
                    "left_control": {"x": 0.0, "y": 0.0},
                    "right_control": {"x": 0.0, "y": 0.0},
                    "values": [float(val)]}

        entry = {
            "id": uid(),
            "material_id": "",
            "property_type": prop,
            "keyframe_list": [kf(t, v) for t, v in points],
        }
        kfs = [k for k in (seg.get("common_keyframes") or [])
               if k.get("property_type") != prop]
        kfs.append(entry)
        seg["common_keyframes"] = kfs
        return seg

    # ------------------------------------------------------------ 写回
    def normalize_paths(self):
        """把素材路径统一成正斜杠（剪映自己写的都是 `E:/...`），返回改了几条。"""
        n = 0
        for key in ("videos", "audios"):
            for m in self.mats.get(key, []):
                p = m.get("path")
                if p and "\\" in p:
                    m["path"] = jycore.slash(p)
                    n += 1
        return n

    def commit(self, backup_first=True):
        """自检 + 加密写回。原有片段一个都不能少、时长不能被改。"""
        if self.doc is None:
            raise RuntimeError("还没 open()")
        tracks = [(t.get("type"), len(t.get("segments") or []))
                  for t in self.doc["tracks"]]
        ids = [s["id"] for t in self.doc["tracks"]
               for s in t.get("segments") or []]
        missing = [i for i in self._snapshot["seg_ids"] if i not in ids]
        if missing:
            raise RuntimeError("原有片段丢了：%s" % missing)
        if tracks[:len(self._snapshot["tracks"])] != self._snapshot["tracks"]:
            raise RuntimeError("原有轨道被改动：%s -> %s"
                               % (self._snapshot["tracks"], tracks))
        if self.doc["duration"] != self._snapshot["duration"]:
            raise RuntimeError("总时长被改了")

        size, bak = jycore.write(self.dir, self.doc, self.work_dir,
                                 backup_first=backup_first)
        self.open()
        return {"bytes": size, "backup": bak, "tracks": tracks}
