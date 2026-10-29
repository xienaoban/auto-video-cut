# -*- coding: utf-8 -*-
"""
配音合成：edge-tts（微软免费音色）+ ffmpeg 去首尾静音。

除了音频，还会落一份词级时间戳（`<hash>.words.json`），供逐字高亮、SRT 导出、
以及"这句到底念了多久"的精确判断。取时间戳用的是 edge-tts 的 WordBoundary 事件，
不是事后对齐——所以它和音频是同一趟请求出来的，天然同源。

归属与依赖（三条都影响你什么时候会踩坑）：
    音色是微软 Azure 的神经语音（`zh-CN-XiaoyiNeural` 这套命名即来自 Azure）。
    调用走的是 Edge 浏览器「大声朗读」那条通道，由 edge_tts 库模拟浏览器请求，
    不需要任何密钥。代价是它属于消费级通道：
      1. 必须联网，断网不可用；批量跑时可能遇到软性限流（有缓存，重跑不会重复请求）
      2. 没有 SLA、没有速率承诺。微软改协议时要靠升级 edge_tts 跟上——
         本仓库把它 vendor 在 .vendor/ 里，出问题就换那个目录的版本
      3. 合成完的音频落在本地，之后不再联网
    要离线、要商用授权、要克隆音色，换 Azure 正式 TTS 或本地模型
    （CosyVoice / F5-TTS）；后者的代价是词级时间戳得自己做对齐，
    "音频和时间戳同一趟出来"这个优势会丢。

音色参考（社区常用）：
    zh-CN-XiaoyiNeural    晓伊，偏少女音，鲸鱼娘桌宠就用这个，配 rate=-10% 更自然
    zh-CN-XiaoxiaoNeural  晓晓，标准女声，活泼
    zh-CN-YunxiNeural     云希，男声，B站「AI接管账号」实测号用的
    zh-CN-YunjianNeural   云健，偏解说腔
"""
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, ".vendor")
if os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

TTS_DIR = os.path.join(HERE, "work", "tts")

# True 时忽略已有缓存，全部重新合成（build.py 的 --no-cache 会打开它）
NO_CACHE = False

# _trim() 用 start_silence=0.04，也就是开头会保留 40ms 静音。
# 词级时间戳是相对原始音频算的，裁剪后要减掉"被切走的那部分"，
# 所以实际位移 = 原始前导静音 - 40ms（见 _shift_words）。
KEEP_HEAD_MS = 40.0

_SIL_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SIL_END = re.compile(r"silence_end:\s*(\d+(?:\.\d+)?)")



def set_workdir(path):
    """把配音缓存指到指定目录（通常是 <工作目录>/.jybuild/tts）。"""
    global TTS_DIR
    TTS_DIR = os.path.join(path, "tts")
    os.makedirs(TTS_DIR, exist_ok=True)
    return TTS_DIR


def set_no_cache(on=True):
    """忽略已有缓存。用于补全旧缓存缺失的词级时间，或改了音色参数后强制重来。"""
    global NO_CACHE
    NO_CACHE = bool(on)
    return NO_CACHE


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


def _lead_silence_ms(path):
    """测原始音频开头有多少静音（毫秒）。没有就返回 0。"""
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", path, "-af",
         "silencedetect=noise=-45dB:d=0.04", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    at_zero = False
    for line in (p.stderr or "").splitlines():
        m = _SIL_START.search(line)
        if m:
            at_zero = float(m.group(1)) <= 0.001
            continue
        m = _SIL_END.search(line)
        if m and at_zero:
            return float(m.group(1)) * 1000.0
    return 0.0


def _shift_words(words, lead_ms):
    """把词级时间从"原始音频"搬到"裁剪后的音频"上。

    _trim() 会切掉前导静音但保留 40ms，所以整体前移 lead - 40ms。
    这点误差最多 40ms，肉眼看不出，但如果哪天改了 _trim，这里要跟着改。
    """
    d = lead_ms - KEEP_HEAD_MS
    if d <= 0:
        return words
    out = []
    for w in words:
        out.append({
            "text": w["text"],
            "start": max(0.0, w["start"] - d),
            "end": max(0.0, w["end"] - d),
        })
    return out


def _clamp_words(words, duration_s):
    """夹进音频真实时长内，并保证 start <= end。"""
    limit = max(0.0, duration_s * 1000.0)
    out = []
    for w in words:
        s = min(w["start"], limit)
        e = min(w["end"], limit)
        if e < s:
            e = s
        out.append({"text": w["text"], "start": round(s, 1), "end": round(e, 1)})
    return out


def _read_words(path):
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f) or {}).get("words") or []
    except Exception:
        return []


def _write_words(path, payload):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


async def _synth_one(text, voice, rate, raw_path):
    """一趟请求同时拿音频和词边界，天然同源。

    offset / duration 是 100 纳秒单位（1ms = 10000），转成毫秒。
    """
    import edge_tts
    words = []
    with open(raw_path, "wb") as f:
        async for msg in edge_tts.Communicate(
                text, voice, rate=rate, boundary="WordBoundary").stream():
            if msg["type"] == "audio":
                f.write(msg["data"])
            elif msg["type"] in ("WordBoundary", "SentenceBoundary"):
                off = msg.get("offset", 0)
                dur = msg.get("duration", 0)
                words.append({"text": msg.get("text", ""),
                              "start": off / 10000.0,
                              "end": (off + dur) / 10000.0})
    return words


async def _synth_all(jobs, voice, rate):
    """jobs = [(text, raw_path, hash)]，返回 {hash: words}。"""
    got = {}

    async def one(text, raw, h):
        got[h] = await _synth_one(text, voice, rate, raw)

    await asyncio.gather(*[one(t, r, h) for t, r, h in jobs])
    return got


def synthesize(texts, voice="zh-CN-XiaoyiNeural", rate="+0%"):
    """批量合成，返回 [(wav路径, 秒数, 词列表)]，顺序与输入一致。带缓存。

    词列表形如 [{"text":"这","start":12.3,"end":180.0}]，单位毫秒，
    相对**裁剪后**音频的起点。旧缓存（没有 .words.json）会返回空列表，
    不重跑也能出片，只是没有词级时间。
    """
    if not available():
        raise RuntimeError("edge-tts 不可用，请确认 .vendor 目录存在")
    os.makedirs(TTS_DIR, exist_ok=True)

    # 同一个句子可能出现多次，按 hash 去重，避免并发写同一个文件
    item = {}
    for t in texts:
        h = _key(t, voice, rate)
        item[h] = (t,
                   os.path.join(TTS_DIR, h + ".wav"),
                   os.path.join(TTS_DIR, h + ".words.json"))

    missing = {h: v for h, v in item.items()
               if NO_CACHE or not (os.path.exists(v[1]) and _probe(v[1]) > 0)}

    if missing:
        jobs = [(v[0], os.path.join(TTS_DIR, h + ".raw.mp3"), h)
                for h, v in missing.items()]
        words_by_h = asyncio.run(_synth_all(jobs, voice, rate))
        for h, (text, wav, wj) in missing.items():
            raw = os.path.join(TTS_DIR, h + ".raw.mp3")
            lead = _lead_silence_ms(raw)
            _trim(raw, wav)
            dur = _probe(wav)
            words = _clamp_words(_shift_words(words_by_h.get(h, []), lead), dur)
            _write_words(wj, {"text": text, "voice": voice, "rate": rate,
                              "duration": round(dur, 3), "words": words})
            try:
                os.remove(raw)
            except OSError:
                pass

    out = []
    for t in texts:
        _, wav, wj = item[_key(t, voice, rate)]
        out.append((wav, _probe(wav), _read_words(wj)))
    return out

