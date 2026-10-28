# -*- coding: utf-8 -*-
"""
素材预规整：把任意视频/图片统一成目标画布尺寸、恒定帧率、H.264 的片段。
这样剪映里所有片段的缩放恒为 1，永远不会出现画幅错位。

图片在这里顺便做成"静帧 + 缓慢推拉"的片段——这就是最朴素的一种程序动画。
"""
import hashlib
import math
import os
import subprocess

from jylib import ffprobe

HERE = os.path.dirname(os.path.abspath(__file__))
# 缓存目录。默认放在工具目录下的 work/，但调用方应用 set_workdir() 指向
# 自己的工作目录——本工具作为只读组件被复用时，不该往工具目录里写东西。
WORK = os.path.join(HERE, "work", "normalized")
CACHE_VERSION = "3"   # 改动渲染逻辑时自增，强制重建缓存


def set_workdir(path):
    """把规整缓存指到指定目录（通常是 <工作目录>/.jybuild/normalized）。"""
    global WORK
    WORK = os.path.join(path, "normalized")
    os.makedirs(WORK, exist_ok=True)
    return WORK


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError("ffmpeg 失败:\n%s\n%s" % (" ".join(cmd[-6:]), p.stderr[-1200:]))
    return p


def _cache_key(*parts):
    h = hashlib.md5("|".join(str(p) for p in (CACHE_VERSION,) + parts).encode("utf-8")).hexdigest()[:16]
    return os.path.join(WORK, h + ".mp4")


def normalize(src, width, height, fps=30, fit="cover", tag=""):
    """视频规整。fit: cover=裁切铺满 / contain=留黑边。返回规整后路径。"""
    if not os.path.exists(src):
        raise FileNotFoundError(src)
    dst = _cache_key("v", src, os.path.getmtime(src), width, height, fps, fit, tag)
    if os.path.exists(dst):
        return dst
    os.makedirs(WORK, exist_ok=True)

    if fit == "cover":
        vf = ("scale=%d:%d:force_original_aspect_ratio=increase,"
              "crop=%d:%d,fps=%d,setsar=1" % (width, height, width, height, fps))
    else:
        vf = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
              "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,fps=%d,setsar=1"
              % (width, height, width, height, fps))

    _run(["ffmpeg", "-y", "-v", "error", "-i", src,
          "-vf", vf,
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
          "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
          "-movflags", "+faststart", dst])
    return dst


def still(image, duration, width, height, fps=30, kenburns=True, zoom=0.12):
    """图片 → 带缓慢推拉的静帧视频。"""
    if not os.path.exists(image):
        raise FileNotFoundError(image)
    dst = _cache_key("i", image, os.path.getmtime(image), duration,
                     width, height, fps, kenburns, zoom)
    if os.path.exists(dst):
        return dst
    os.makedirs(WORK, exist_ok=True)

    # 多给 2 帧，保证成品时长不短于请求时长（否则剪映里会出现源区间越界）
    frames = max(2, int(math.ceil(duration * fps)) + 2)
    # 先放大到画布尺寸的 2 倍再缩，避免 zoompan 抖动
    pre = ("scale=%d:%d:force_original_aspect_ratio=increase,"
           "crop=%d:%d" % (width * 2, height * 2, width * 2, height * 2))

    if kenburns:
        z = "min(1+%.4f*on/%d,%.3f)" % (zoom, frames, 1 + zoom)
        vf = (pre + ",zoompan=z='%s':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              ":d=%d:s=%dx%d:fps=%d,setsar=1" % (z, frames, width, height, fps))
        _run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", image,
              "-vf", vf, "-frames:v", str(frames),
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
              "-pix_fmt", "yuv420p", dst])
    else:
        vf = pre + ",scale=%d:%d,setsar=1" % (width, height)
        _run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", image,
              "-vf", vf, "-r", str(fps), "-frames:v", str(frames),
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
              "-pix_fmt", "yuv420p", dst])
    return dst


def clip_duration(path):
    return ffprobe(path)["duration"]
