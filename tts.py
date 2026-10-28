# -*- coding: utf-8 -*-
"""
配音合成：edge-tts（微软免费音色）+ ffmpeg 去首尾静音。

音色参考（社区常用）：
    zh-CN-XiaoyiNeural    晓伊，偏少女音，鲸鱼娘桌宠就用这个，配 rate=-10% 更自然
    zh-CN-XiaoxiaoNeural  晓晓，标准女声，活泼
    zh-CN-YunxiNeural     云希，男声，B站「AI接管账号」实测号用的
    zh-CN-YunjianNeural   云健，偏解说腔
"""
import asyncio
import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, ".vendor")
if os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

TTS_DIR = os.path.join(HERE, "work", "tts")


def set_workdir(path):
    """把配音缓存指到指定目录（通常是 <工作目录>/.jybuild/tts）。"""
    global TTS_DIR
    TTS_DIR = os.path.join(path, "tts")
    os.makedirs(TTS_DIR, exist_ok=True)
    return TTS_DIR


def available():
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return False


def _key(text, voice, rate):
    h = hashlib.md5(("%s|%s|%s" % (text, voice, rate)).encode("utf-8")).hexdigest()[:20]
    return h


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def _trim(src, dst):
    """去掉首尾静音，统一成 48k 单声道 wav。"""
    af = ("silenceremove=start_periods=1:start_silence=0.04:start_threshold=-45dB,"
          "areverse,"
          "silenceremove=start_periods=1:start_silence=0.04:start_threshold=-45dB,"
          "areverse")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src,
                    "-af", af, "-ar", "48000", "-ac", "1", dst],
                   capture_output=True, check=True)


async def _synth_one(text, voice, rate, raw_path):
    import edge_tts
    await edge_tts.Communicate(text, voice, rate=rate).save(raw_path)


async def _synth_all(jobs, voice, rate):
    await asyncio.gather(*[_synth_one(t, voice, rate, p) for t, p, _ in jobs])


def synthesize(texts, voice="zh-CN-XiaoyiNeural", rate="+0%"):
    """批量合成，返回 [(wav路径, 秒数)]，顺序与输入一致。带缓存。"""
    if not available():
        raise RuntimeError("edge-tts 不可用，请确认 .vendor 目录存在")
    os.makedirs(TTS_DIR, exist_ok=True)

    results = [None] * len(texts)
    jobs = []
    for i, t in enumerate(texts):
        h = _key(t, voice, rate)
        wav = os.path.join(TTS_DIR, h + ".wav")
        if os.path.exists(wav) and _probe(wav) > 0:
            results[i] = (wav, _probe(wav))
        else:
            jobs.append((t, os.path.join(TTS_DIR, h + ".raw.mp3"), (i, wav)))

    if jobs:
        asyncio.run(_synth_all(jobs, voice, rate))
        for _, raw, (i, wav) in jobs:
            _trim(raw, wav)
            try:
                os.remove(raw)
            except OSError:
                pass
            results[i] = (wav, _probe(wav))

    return results
