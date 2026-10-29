# -*- coding: utf-8 -*-
"""
环境自检。开跑前先来一遍，比生成到一半报错省事。

用法：
    python doctor.py
    python doctor.py --json

退出码：0=全部正常，1=有阻塞问题。
"""
import json
import os
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True   # 工具目录保持只读，不生成 __pycache__

import jylib
import jycore
import jycrypto

HERE = os.path.dirname(os.path.abspath(__file__))
DRAFTC_DIR = os.path.join(HERE, "utils", "jy-draftc")
DRAFTC_REPO = "https://github.com/wenshui330/jy-draftc.git"

ASSETS_NEEDED = ("skeleton.json", "video_material.json", "text_material.json",
                 "audio_material.json", "video_segment.json", "text_segment.json",
                 "audio_segment.json")


def has_cmd(name):
    return shutil.which(name) is not None


def ffmpeg_info():
    """ffmpeg 的可执行路径与版本行。AVC_FFMPEG_DIR 优先，否则走 PATH。"""
    ff = jycore.tool("ffmpeg")
    out = subprocess.run([ff, "-version"], capture_output=True, text=True,
                         errors="replace")
    ver = (out.stdout or "").splitlines()[0] if out.stdout else "?"
    return ff, ver.split(" Copyright")[0].strip()


def ensure_draftc(auto=True):
    """utils/jy-draftc 有没有当前剪映版本的源码；没有就拉一份。

    这个仓库不是依赖，是参考和备选：里面有草稿结构升降级（draft_convert.cpp）、
    无头导出（video_export.cpp）和结构声明。加解密我们已经自己实现了，
    但排查字段、以后想不开剪映直接出片，还得靠它。
    """
    src = os.path.join(DRAFTC_DIR, "src", jycore.VERSION)
    if os.path.isdir(src):
        return True, src
    if not has_cmd("git"):
        return False, "没装 git，拉不了参考仓库（不影响主流程）"
    if os.path.isdir(os.path.join(DRAFTC_DIR, ".git")):
        # 已有旧副本，直接更新
        p = subprocess.run(["git", "-C", DRAFTC_DIR, "pull", "--ff-only"],
                           capture_output=True, text=True)
        msg = (p.stdout or p.stderr or "").strip().splitlines()
        return (os.path.isdir(src),
                "更新失败：" + (msg[-1] if msg else "未知原因"))
    if not auto:
        return False, "未下载，需要时执行：git clone %s %s" % (DRAFTC_REPO, DRAFTC_DIR)
    os.makedirs(os.path.dirname(DRAFTC_DIR), exist_ok=True)
    p = subprocess.run(["git", "clone", "--depth", "1", DRAFTC_REPO, DRAFTC_DIR],
                       capture_output=True, text=True)
    if p.returncode != 0:
        tail = ((p.stderr or p.stdout or "").strip().splitlines() or ["未知错误"])[-1]
        return False, "拉取失败：%s（不影响主流程）" % tail
    return os.path.isdir(src), "已拉取到 utils/jy-draftc"


def main():
    rows = []

    def add(name, ok, detail, blocking=True):
        rows.append({"item": name, "ok": ok, "detail": detail,
                     "blocking": blocking})

    py = "%d.%d.%d" % sys.version_info[:3]
    add("Python", sys.version_info >= (3, 8), py)

    try:
        ff, ver = ffmpeg_info()
        add("ffmpeg", True, "%s｜%s" % (ver, ff))
    except Exception as e:  # noqa: BLE001
        add("ffmpeg", False, str(e).splitlines()[0])
    try:
        add("ffprobe", True, jycore.tool("ffprobe"))
    except Exception as e:  # noqa: BLE001
        add("ffprobe", False, str(e).splitlines()[0])

    # 剪映版本：草稿字段跟着版本走，对不上就不保证能正确写
    ok, msg = jycore.check()
    add("剪映版本", ok, msg)

    # 加密通道：能不能加载 dll 里的加解密导出函数
    if ok:
        try:
            jycrypto.set_app_dir(jycore.app_dir())
            # 探针要长于 16 字节：dll 的短字符串分支（SSO）不保证往返，
            # 实际草稿都是几 KB，走的是长串分支。
            probe = '{"probe":"加解密通道自检，这条字符串要足够长"}'.encode("utf-8")
            blob = jycrypto.encrypt(probe)
            back, good = jycrypto.decrypt(blob)
            good = bool(good and back == probe)
            add("草稿加解密", good,
                "往返校验通过（%d 字节明文 → %d 字节密文）" % (len(probe), len(blob))
                if good else "往返不一致，密文通道不可用")
        except Exception as e:  # noqa: BLE001
            add("草稿加解密", False, "加载 videoeditor.dll 失败：%s" % e)
    else:
        add("草稿加解密", False, "剪映版本不对，跳过", blocking=False)

    try:
        import tts
        ok_tts = tts.available()
        add("edge-tts", ok_tts,
            "已就绪 (.vendor)" if ok_tts
            else "未安装，配音会退回「剪映朗读」模式（按字数估算时长）",
            blocking=False)
    except Exception as e:  # noqa: BLE001
        add("edge-tts", False, "导入失败: %s" % e, blocking=False)

    missing = [a for a in ASSETS_NEEDED
               if not os.path.exists(os.path.join(jylib.ASSETS, a))]
    add("字段模板 assets/", not missing,
        "齐全" if not missing else "缺失: " + ", ".join(missing))

    # 内置片段模板：草稿里没有同类片段时靠它顶上，缺了不影响建草稿但会卡住加字幕
    tf = os.path.join(jylib.ASSETS, "jy_template.json")
    if os.path.exists(tf):
        try:
            with open(tf, encoding="utf-8") as f:
                tpl = json.load(f)
            kinds = [k for k in ("text", "audio") if k in tpl]
            add("片段模板", set(kinds) == {"text", "audio"},
                "%s（%s 字节）" % ("/".join(kinds) or "空", os.path.getsize(tf)))
        except (OSError, ValueError) as e:
            add("片段模板", False, "读不动 %s: %s" % (tf, e))
    else:
        add("片段模板", False, "缺 %s，草稿里没有字幕/音频样本时加不了" % tf)

    root = jycore.draft_root()
    if os.environ.get(jycore.ENV_DRAFT_ROOT):
        dsrc = jycore.ENV_DRAFT_ROOT
    elif os.environ.get("JIANYING_DRAFT_ROOT"):
        dsrc = "JIANYING_DRAFT_ROOT（旧名）"
    else:
        dsrc = "config.json / 默认位置"
    add("剪映草稿目录", bool(root),
        "%s（来源：%s）" % (root, dsrc) if root
        else "未找到，需要配置 config.json 的 draft_root 或 %s" % jycore.ENV_DRAFT_ROOT)

    # 工程目录：硬约束。没有它，配音和配图不知道往哪放，宁可不写草稿
    proj_err = None
    try:
        proj = jycore.project_dir()
        add("工程目录", True, "%s（含 %s/）" % (proj, jycore.VIDEO_SUBDIR))
    except Exception as e:  # noqa: BLE001
        proj_err = str(e).splitlines()[0]
        add("工程目录", False, proj_err)

    running = jycore.jianying_running()
    add("剪映进程", not running,
        "剪映正在运行，写入前请完全退出（含托盘 JianyingProTray.exe）" if running
        else "未运行，可以安全写入")

    # 参考仓库：缺就拉，拉不到只 WARN
    got, detail = ensure_draftc(auto="--no-fetch" not in sys.argv)
    add("jy-draftc 参考", got, detail, blocking=False)

    font = None
    for c in jylib.FONT_FALLBACKS:
        if os.path.exists(c):
            font = c
            break
    add("中文字体兜底", bool(font), font or "未找到系统中文无衬线字体",
        blocking=False)

    if "--json" in sys.argv:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        print("环境自检\n" + "-" * 68)
        for r in rows:
            mark = "OK  " if r["ok"] else ("FAIL" if r["blocking"] else "WARN")
            print("[%s] %-14s %s" % (mark, r["item"], r["detail"]))
        bad = [r for r in rows if not r["ok"] and r["blocking"]]
        print("-" * 68)
        print("阻塞问题 %d 个" % len(bad) if bad else "全部正常，可以开工")
        if proj_err:
            print("\n" + jycore.PROJECT_HINT)

    return 1 if any(not r["ok"] and r["blocking"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
