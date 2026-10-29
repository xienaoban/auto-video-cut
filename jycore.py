# -*- coding: utf-8 -*-
"""草稿文件层：定位剪映、读草稿、备份、写回。

这一层只关心「文件在哪、怎么读、怎么写回去」，不理解片段和素材的语义。
上层 jyedit 用它拿到的 dict 做增删改。

三条踩出来的硬规则，写在这里免得再犯：

1. 编辑器加载的是 `10月30日/Timelines/<main_timeline_id>/draft_content.json`，
   顶层那份只是镜像。只改顶层，剪映打开时会用时间轴那份覆盖回去，
   表现成「改动被回滚」。
2. 所以写回必须两处同步，连同各自的 `.bak` 和 `template-2.tmp`。
3. 剪映运行时会锁文件、也会在退出时覆盖，写之前必须确认进程已退出。
"""
import json
import os
import shutil
import subprocess
import time

import jycrypto

# 本工具只针对这个版本验证过。剪映一升级，草稿字段可能变，所以卡死版本。
VERSION = "11.6.0.14552"
VERSION_HINT = (
    'winget download --id ByteDance.JianyingPro --exact --version %s '
    '--download-directory "C:\\Users\\你的用户名\\Downloads\\"' % VERSION)

# 可选环境变量：能自动探测到，但多版本并存、PATH 里装了多个 ffmpeg 时，
# 用它们锁定更省事。写了优先，不写就自动探测。
ENV_JIANYING_DIR = "AVC_JIANYING_DIR"
ENV_DRAFT_ROOT = "AVC_DRAFT_ROOT"
ENV_FFMPEG_DIR = "AVC_FFMPEG_DIR"

CONTENT_NAME = "draft_content.json"
MIRROR_NAMES = ("draft_content.json.bak", "template-2.tmp")


# ---------------------------------------------------------------- 剪映安装

def apps_root():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
    return os.path.join(base, "JianyingPro", "Apps")


def versions():
    """已安装的剪映版本目录名，按版本号从大到小。"""
    root = apps_root()
    if not os.path.isdir(root):
        return []
    out = []
    for name in os.listdir(root):
        if os.path.exists(os.path.join(root, name, "videoeditor.dll")):
            out.append(name)
    return sorted(out, key=parse_version, reverse=True)


def parse_version(name):
    parts = []
    for p in name.split("."):
        parts.append(int(p) if p.isdigit() else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def app_dir(version=None):
    """返回剪映程序目录（含 videoeditor.dll）。

    优先级：显式 version > AVC_JIANYING_DIR > 扫描 %LOCALAPPDATA%\\JianyingPro\\Apps
    """
    env = os.environ.get(ENV_JIANYING_DIR)
    if env and not version:
        p = os.path.normpath(os.path.expandvars(os.path.expanduser(env)))
        if not os.path.exists(os.path.join(p, "videoeditor.dll")):
            raise RuntimeError(
                "%s=%s 里没有 videoeditor.dll，不像剪映程序目录。\n"
                "指向含 videoeditor.dll 的那一层，例如\n"
                "  ...\\JianyingPro\\Apps\\%s" % (ENV_JIANYING_DIR, p, VERSION))
        return p

    root = apps_root()
    if version:
        p = os.path.join(root, version)
        if os.path.exists(os.path.join(p, "videoeditor.dll")):
            return p
        raise RuntimeError("没装剪映 %s。\n已装：%s\n需要的话自行下载安装：\n  %s"
                           % (version, ", ".join(versions()) or "无", VERSION_HINT))
    if os.path.exists(os.path.join(root, VERSION, "videoeditor.dll")):
        return os.path.join(root, VERSION)
    vs = versions()
    if not vs:
        raise RuntimeError("找不到剪映安装目录 %s。\n需要时自行下载安装：\n  %s"
                           % (root, VERSION_HINT))
    return os.path.join(root, vs[0])


def check(version=VERSION):
    """版本自检，返回 (ok, 说明文本)。"""
    env = os.environ.get(ENV_JIANYING_DIR)
    if env:
        try:
            p = app_dir()
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        name = os.path.basename(p.rstrip("\\/")) or p
        if version and version not in name:
            return False, ("%s 指向的是「%s」，不是锚定的 %s。\n"
                           "草稿字段跟着版本走，对不上不保证正确。\n"
                           "改回 %s 或换掉这个变量。"
                           % (ENV_JIANYING_DIR, name, version, VERSION_HINT))
        return True, "剪映 %s（%s 指定）" % (name, ENV_JIANYING_DIR)

    try:
        vs = versions()
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if not vs:
        return False, "没找到任何剪映安装。\n本工具锚定 %s，装好后重试：\n  %s" % (VERSION, VERSION_HINT)
    if version and version not in vs:
        return False, ("本工具锚定剪映 %s，你装的是 %s。\n草稿字段跟着版本走，"
                       "版本对不上不保证正确。\n需要的话自行下载安装：\n  %s"
                       % (version, ", ".join(vs), VERSION_HINT))
    return True, "剪映 %s%s" % (version or vs[0],
                                "" if len(vs) == 1 else "（装了 %d 个版本，取最高）" % len(vs))


# ---------------------------------------------------------------- 草稿定位

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def tool(name):
    """ffmpeg / ffprobe 的可执行路径。AVC_FFMPEG_DIR 优先，否则走 PATH。"""
    d = os.environ.get(ENV_FFMPEG_DIR)
    if d:
        d = os.path.normpath(os.path.expandvars(os.path.expanduser(d)))
        if not os.path.isdir(d):
            raise RuntimeError("%s=%s 不是目录。\n它应该指向含 ffmpeg.exe 的目录。"
                               % (ENV_FFMPEG_DIR, d))
        for cand in (name + ".exe", name):
            p = os.path.join(d, cand)
            if os.path.isfile(p):
                return p
        raise RuntimeError("%s=%s 里没有 %s.exe。\n它应该指向含 ffmpeg.exe 的目录。"
                           % (ENV_FFMPEG_DIR, d, name))
    p = shutil.which(name)
    if not p:
        raise RuntimeError("PATH 里找不到 %s。\n装好后加入 PATH，或者设 %s "
                           "指向它所在的目录。" % (name, ENV_FFMPEG_DIR))
    return p


def draft_root(explicit=None):
    """剪映草稿根目录。

    优先级：显式参数 > AVC_DRAFT_ROOT > JIANYING_DRAFT_ROOT > config.json > 默认位置。
    """
    if explicit:
        return os.path.normpath(os.path.expandvars(os.path.expanduser(explicit)))
    cands = []
    for key in (ENV_DRAFT_ROOT, "JIANYING_DRAFT_ROOT"):
        env = os.environ.get(key)
        if env:
            cands.append(os.path.expandvars(os.path.expanduser(env)))
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                p = (json.load(f) or {}).get("draft_root")
            if p and not p.startswith("<"):
                cands.append(os.path.expandvars(os.path.expanduser(p)))
        except Exception:  # noqa: BLE001
            pass
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
    for app in ("JianyingPro", "JianyingPro Beta", "CapCut"):
        cands.append(os.path.join(base, app, "User Data", "Projects",
                                  "com.lveditor.draft"))
    for c in cands:
        if c and os.path.exists(os.path.join(c, "root_meta_info.json")):
            return os.path.normpath(c)
    for c in cands:
        if c and os.path.isdir(c):
            return os.path.normpath(c)
    return None


def draft_dir(name, root=None):
    r = root or draft_root()
    if not r:
        raise RuntimeError("找不到剪映草稿目录，请在 config.json 里写 draft_root。")
    p = os.path.join(r, name)
    if not os.path.isdir(p):
        raise RuntimeError("没有叫「%s」的草稿。现有：%s"
                           % (name, ", ".join(list_drafts(r)) or "无"))
    return p


def list_drafts(root=None):
    r = root or draft_root()
    if not r or not os.path.isdir(r):
        return []
    return sorted(n for n in os.listdir(r)
                  if os.path.isdir(os.path.join(r, n)) and not n.startswith("."))


def timeline_dir(d):
    """草稿正在用的时间轴目录。

    11.x 一个草稿可以有多个时间轴，编辑器打开的是 project.json 里的
    main_timeline_id。没有 project.json（工具刚建、剪映还没打开过）时返回 None。
    """
    tl_root = os.path.join(d, "Timelines")
    proj = os.path.join(tl_root, "project.json")
    if os.path.exists(proj):
        try:
            with open(proj, encoding="utf-8") as f:
                pid = (json.load(f) or {}).get("main_timeline_id")
            if pid and os.path.isdir(os.path.join(tl_root, pid)):
                return os.path.join(tl_root, pid)
        except Exception:  # noqa: BLE001
            pass
    if os.path.isdir(tl_root):
        subs = [n for n in os.listdir(tl_root)
                if os.path.isdir(os.path.join(tl_root, n))]
        if len(subs) == 1:
            return os.path.join(tl_root, subs[0])
    return None


def content_path(d):
    """读取用的权威文件：优先时间轴目录那份。"""
    tl = timeline_dir(d)
    if tl and os.path.exists(os.path.join(tl, CONTENT_NAME)):
        return os.path.join(tl, CONTENT_NAME)
    return os.path.join(d, CONTENT_NAME)


def write_targets(d):
    """写回时必须同步的全部位置（顶层 + 时间轴，各主文件、.bak、template-2.tmp）。"""
    out = [os.path.join(d, n) for n in (CONTENT_NAME,) + MIRROR_NAMES]
    tl = timeline_dir(d)
    if tl:
        out += [os.path.join(tl, n) for n in (CONTENT_NAME,) + MIRROR_NAMES]
    return [p for p in out if os.path.exists(p)
            or os.path.basename(p) == CONTENT_NAME]


# ---------------------------------------------------------------- 视频工程目录

# 本次视频工程的目录，由用户在环境里指定。工具不猜——AI 一个 session 里同时看得见
# 草稿目录、素材目录、工具目录，猜错一次就把配音写到别人家去了。宁可停下报错。
ENV_PROJECT = "AVC_PROJECT_DIR"
VIDEO_SUBDIR = "视频"

PROJECT_HINT = (
    "本工具不猜工程目录，必须由环境变量 %s 指定：\n"
    "  Git Bash:    export %s=/e/project-video/生物辞典5\n"
    "  PowerShell:  $env:%s = 'E:\\project-video\\生物辞典5'\n"
    "  WorkBuddy:   设置 → 环境变量，配一次对所有命令生效\n"
    "目录里必须有 %s/ 子目录，配音、配图都收进这个目录。"
    % (ENV_PROJECT, ENV_PROJECT, ENV_PROJECT, VIDEO_SUBDIR))


def project_dir(explicit=None):
    """本次视频工程目录。只认环境变量（或显式传入），读不到直接报错。"""
    raw = explicit or os.environ.get(ENV_PROJECT)
    if not raw:
        raise RuntimeError("没有设置环境变量 %s。\n%s" % (ENV_PROJECT, PROJECT_HINT))
    p = os.path.normpath(os.path.expandvars(os.path.expanduser(raw)))
    if not os.path.isdir(p):
        raise RuntimeError("%s=%s 指向的目录不存在。\n%s"
                           % (ENV_PROJECT, raw, PROJECT_HINT))
    if not os.path.isdir(os.path.join(p, VIDEO_SUBDIR)):
        raise RuntimeError(
            "%s=%s 里没有 %s/ 子目录，看着不像视频工程目录。\n"
            "确认路径写对了，或者先建 %s/ 把素材放进去。\n%s"
            % (ENV_PROJECT, p, VIDEO_SUBDIR, VIDEO_SUBDIR, PROJECT_HINT))
    return p


# ---------------------------------------------------------------- 素材落盘

# 本次视频工程的素材目录名（和用户工程里的「视频/」平级）
MEDIA_DIRS = {"audio": "音频", "image": "图片"}


def slash(p):
    """剪映草稿里的路径一律写正斜杠（它自己生成的素材就是 `E:/...`）。"""
    return (p or "").replace("\\", "/")


def media_dir(work_dir, kind):
    """<工程目录>/音频 或 <工程目录>/图片。"""
    return os.path.join(os.path.normpath(work_dir), MEDIA_DIRS[kind])


def _same_bytes(a, b):
    if not (os.path.exists(a) and os.path.exists(b)):
        return False
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    import hashlib

    def digest(p):
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    return digest(a) == digest(b)


def import_media(src, work_dir, kind, filename=None):
    """把素材复制进本次视频工程目录，返回工程内的绝对路径。

    硬规矩：草稿里引用的路径必须落在本次视频的工作目录里。
    指向 C 盘草稿缓存（textReading）、剪映缓存、工具仓库 tmp 的路径，
    换个机器、清个缓存就断了，导出直接找不到素材。
    """
    src = os.path.normpath(os.path.expandvars(os.path.expanduser(src)))
    if not os.path.isfile(src):
        raise RuntimeError("素材不存在：%s" % src)
    dest_dir = media_dir(work_dir, kind)
    os.makedirs(dest_dir, exist_ok=True)

    name = filename or os.path.basename(src)
    dest = os.path.join(dest_dir, name)
    if os.path.exists(dest):
        if _same_bytes(src, dest):
            return slash(dest)
        stem, ext = os.path.splitext(name)
        n = 2
        while os.path.exists(os.path.join(dest_dir, "%s_%d%s" % (stem, n, ext))):
            n += 1
        dest = os.path.join(dest_dir, "%s_%d%s" % (stem, n, ext))
    shutil.copy2(src, dest)
    return slash(dest)


# ---------------------------------------------------------------- 读写

def read(path):
    """读一个草稿文件，明文密文都行。"""
    with open(path, "rb") as f:
        raw = f.read()
    if jycrypto.looks_plain(raw):
        return json.loads(raw.decode("utf-8"))
    plain, ok = jycrypto.decrypt(raw)
    if not ok or not plain:
        raise RuntimeError("解密失败，可能不是本机剪映写的：%s" % path)
    return json.loads(plain.decode("utf-8"))


def dump(doc):
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode(doc):
    """序列化 + 加密 + 往返自校验，返回密文。"""
    plain = dump(doc)
    enc = jycrypto.encrypt(plain)
    back, ok = jycrypto.decrypt(enc)
    if not ok or back != plain:
        raise RuntimeError("加解密往返校验失败，已中止（什么都没写）")
    return enc


def jianying_running():
    try:
        out = subprocess.run(["tasklist"], capture_output=True, text=True,
                             encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return False
    low = (out.stdout or "").lower()
    return "jianyingpro" in low or "lveditor" in low


def backup(d, work_dir=None):
    """整目录备份到 <工作目录>/.tmp/backup_<草稿名>_<时间>/，返回备份路径。"""
    work_dir = work_dir or project_dir()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(work_dir, ".tmp",
                        "backup_%s_%s" % (os.path.basename(d), stamp))
    shutil.copytree(d, dest)
    return dest


def write(d, doc, work_dir=None, backup_first=True):
    """加密写回全部位置。返回 (密文长度, 备份路径)。

    调用前请自行确认剪映已退出；这里也会检查一次，开着就拒绝写。
    备份落在工程目录的 .tmp/ 下——没设 AVC_PROJECT_DIR 就直接报错。
    """
    work_dir = work_dir or project_dir()
    if jianying_running():
        raise RuntimeError(
            "剪映还在运行（含托盘 JianyingProTray.exe）。\n"
            "请先完全退出剪映再执行——否则写进去的内容会被它在退出时覆盖掉。")
    enc = encode(doc)
    bak = backup(d, work_dir) if backup_first else None
    for p in write_targets(d):
        tmp = p + ".aitmp"
        with open(tmp, "wb") as f:
            f.write(enc)
        os.replace(tmp, p)
    return len(enc), bak
