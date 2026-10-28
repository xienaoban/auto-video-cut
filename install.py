# -*- coding: utf-8 -*-
"""
把 requirements.txt 里的依赖装进工具目录的 .vendor/（绿色安装，不碰系统 Python）。

用法：
    python install.py                  安装或补齐缺失依赖
    python install.py --check          只检查，不安装（退出码 0=已就绪）
    python install.py --force          删掉 .vendor 后整份重装
    python install.py --index-url URL  指定 pip 源（国内可用清华/阿里镜像）
    python install.py --pip-arg X      额外透传参数给 pip，可重复

退出码：0=成功或已就绪，1=失败。

说明：
  - 只使用运行本脚本的那个解释器（sys.executable），绝不调用系统 PATH 里的
    python / pip，也不写 site-packages。装完只影响 .vendor/，删掉即完全回退。
  - 不装也能用：配音会自动退回「剪映朗读」模式（按字数估算时长）。
  - .vendor 里的编译扩展绑定 Python 版本和平台（如 cp313-win_amd64.pyd），
    换解释器或换机器必须重跑本脚本，不能直接复制目录。
"""
import os
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True   # 工具目录保持只读，不生成 __pycache__

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, ".vendor")
REQ = os.path.join(HERE, "requirements.txt")

MIN_PY = (3, 8)
IMPORT_CHECK = "edge_tts"          # 装完要能 import 的包
STAMP = os.path.join(VENDOR, ".interpreter")   # 记录当时用的是哪个解释器
MIRRORS = {
    "tuna": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple",
    "ustc": "https://pypi.mirrors.ustc.edu.cn/simple",
}


def say(msg=""):
    print(msg, flush=True)


def mark(kind, msg):
    tag = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "step": "==> "}[kind]
    say("[%s] %s" % (tag, msg))


def run(cmd, **kw):
    """跑子进程并原样透传输出，返回 returncode。"""
    kw.setdefault("cwd", HERE)
    try:
        return subprocess.call(cmd, **kw)
    except FileNotFoundError:
        return 127


# ------------------------------------------------------------------ 环境检查
def check_python():
    v = sys.version_info
    ok = (v.major, v.minor) >= MIN_PY
    mark("ok" if ok else "fail", "Python %d.%d.%d  (%s)" % (v[0], v[1], v[2], sys.executable))
    if not ok:
        say("     需要 Python %d.%d 或更高版本。" % MIN_PY)
    return ok


def check_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        mark("ok", "ffmpeg  %s" % p)
        return True
    mark("warn", "ffmpeg 未找到 —— 本脚本不管它，但生成草稿必需，请先装好并加入 PATH")
    return True


def check_requirements():
    if not os.path.exists(REQ):
        mark("fail", "缺少 requirements.txt：%s" % REQ)
        return None
    with open(REQ, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]
    pkgs = [ln for ln in lines if ln and not ln.startswith("#")]
    if not pkgs:
        mark("warn", "requirements.txt 里没有任何依赖，无需安装")
        return []
    mark("ok", "requirements.txt：%s" % ", ".join(pkgs))
    return pkgs


# ---------------------------------------------------------------------- pip
def pip_works():
    return run([sys.executable, "-m", "pip", "--version"],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def ensure_pip():
    if pip_works():
        return True

    mark("warn", "当前解释器没有 pip，尝试用标准库 ensurepip 装一个")
    run([sys.executable, "-m", "ensurepip", "--upgrade"])
    if pip_works():
        mark("ok", "pip 已就绪")
        return True

    mark("fail", "pip 不可用。这个解释器可能是 embeddable 版（精简发行版，不含 pip）")
    say("     可选：换用完整版 Python，或先手动执行")
    say("           %s -m ensurepip --upgrade" % sys.executable)
    say("     再不行就下载 https://bootstrap.pypa.io/get-pip.py 后运行：")
    say("           %s get-pip.py" % sys.executable)
    return False


def pip_install(extra_args):
    cmd = [sys.executable, "-m", "pip", "install",
           "--upgrade", "--target", VENDOR,
           "--no-warn-script-location", "--disable-pip-version-check",
           "-r", REQ] + list(extra_args)
    say("     " + " ".join(cmd))
    say("")
    rc = run(cmd)
    say("")
    return rc == 0


# ------------------------------------------------------------------- .vendor
def vendor_packages():
    """列出 .vendor 里的顶层包目录（不含 dist-info 与缓存）。"""
    if not os.path.isdir(VENDOR):
        return []
    out = []
    for name in sorted(os.listdir(VENDOR)):
        p = os.path.join(VENDOR, name)
        if not os.path.isdir(p):
            continue
        if name.endswith((".dist-info", ".egg-info")) or name in ("bin", "Scripts"):
            continue
        if os.path.exists(os.path.join(p, "__init__.py")) or name in ("bin",):
            out.append(name)
    return out


def _rmtree(path):
    """删目录。Windows 上刚写入的 .pyd 常被杀毒软件短暂占用，
    只读属性也会躲过默认删除，所以这里改权限重试一次而不是静默忽略。"""
    def handler(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except Exception:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=handler)
    else:
        shutil.rmtree(path, onerror=handler)


def purge_vendor():
    if not os.path.isdir(VENDOR):
        return True
    mark("warn", "按 --force 删除现有 .vendor")
    for attempt in range(3):
        _rmtree(VENDOR)
        if not os.path.isdir(VENDOR):
            return True
        time.sleep(0.6)
    return False


def strip_pycache():
    """pip 装包时会顺带生成 __pycache__，删掉能省掉一半体积。"""
    if not os.path.isdir(VENDOR):
        return 0
    freed = 0
    for root, dirs, _files in os.walk(VENDOR):
        for d in list(dirs):
            if d == "__pycache__":
                p = os.path.join(root, d)
                freed += sum(os.path.getsize(os.path.join(p, f))
                             for f in os.listdir(p)
                             if os.path.isfile(os.path.join(p, f)))
                shutil.rmtree(p, ignore_errors=True)
                dirs.remove(d)
    return freed


def verify():
    """把 .vendor 插到 sys.path 最前面，确认真的能 import。"""
    if os.path.isdir(VENDOR) and VENDOR not in sys.path:
        sys.path.insert(0, VENDOR)
    try:
        mod = __import__(IMPORT_CHECK)
    except Exception as e:
        mark("fail", "装完了但 import %s 失败：%s" % (IMPORT_CHECK, e))
        say("     常见原因：包是用别的 Python 版本编的（.pyd 文件名带 cpXXX），")
        say("     先用 --force 重装试试。")
        return False
    ver = getattr(mod, "__version__", "")
    where = os.path.dirname(getattr(mod, "__file__", "") or "")
    inside = os.path.normcase(where).startswith(os.path.normcase(VENDOR))
    mark("ok", "%s %s" % (IMPORT_CHECK, ver or "(版本未知)"))
    if not inside:
        mark("warn", "它其实来自 %s，不是 .vendor —— 说明系统里已经装过同名包" % where)
    return True


# ---------------------------------------------------------------- 解释器记录
def write_stamp():
    """记住是哪个解释器装的。.vendor 里的编译扩展绑定 Python 版本和平台，
    换个解释器就得重装，所以要把这件事留痕。"""
    try:
        os.makedirs(VENDOR, exist_ok=True)
        with open(STAMP, "w", encoding="utf-8") as f:
            f.write("%s\n%d.%d.%d\n" % (sys.executable, sys.version_info[0],
                                        sys.version_info[1], sys.version_info[2]))
    except OSError:
        pass


def read_stamp():
    try:
        with open(STAMP, encoding="utf-8") as f:
            return f.read().strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return None


def same_interpreter(recorded):
    if not recorded:
        return True
    try:
        a = os.path.normcase(os.path.realpath(recorded))
        b = os.path.normcase(os.path.realpath(sys.executable))
    except Exception:
        return True
    return a == b


def already_ready():
    if not os.path.isdir(VENDOR):
        return False
    if not vendor_packages():
        return False
    if not same_interpreter(read_stamp()):
        return False
    sys.path.insert(0, VENDOR)
    try:
        __import__(IMPORT_CHECK)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------- main
def main(argv):
    args = argv[1:]
    do_check = any(a in ("--check", "-c") for a in args)
    force = any(a in ("--force", "-f") for a in args)
    if any(a in ("-h", "--help") for a in args):
        say(__doc__.strip())
        return 0

    pip_extra = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--index-url", "-i") and i + 1 < len(args):
            url = args[i + 1]
            url = MIRRORS.get(url, url)
            pip_extra += ["--index-url", url]
            i += 2
            continue
        if a == "--pip-arg" and i + 1 < len(args):
            pip_extra.append(args[i + 1])
            i += 2
            continue
        i += 1

    say("auto-video-cut 依赖安装")
    say("=" * 68)

    say("\n[1/4] 检查环境")
    if not check_python():
        return 1
    check_ffmpeg()
    pkgs = check_requirements()
    if pkgs is None:
        return 1
    if not pkgs:
        return 0

    rec = read_stamp()
    stale = bool(rec) and not same_interpreter(rec)
    if stale:
        mark("warn", ".vendor 是另一个解释器装的：%s" % rec)
        mark("warn", "编译扩展跨 Python 版本不能复用，这次会用当前解释器重装")

    if do_check:
        ok = already_ready()
        if ok:
            mark("ok", ".vendor 已就绪（%s）" % sys.executable)
        else:
            mark("warn", ".vendor 未就绪，运行 python install.py 安装"
                         "（不装也能跑，配音退回剪映朗读）")
        return 0 if ok else 1

    # 注意顺序：--force 时绝不能先 import，否则 .pyd 被本进程占住就删不掉了
    if not force and not stale and already_ready():
        say("")
        mark("ok", ".vendor 里已经有依赖了，跳过安装（要重装加 --force）")
        strip_pycache()
        if verify():
            write_stamp()
        return 0

    say("\n[2/4] 准备 pip")
    if not ensure_pip():
        return 1

    say("\n[3/4] 安装到 %s" % VENDOR)
    say("     解释器：%s" % sys.executable)
    say("     （只用这个解释器的 pip，不碰系统 PATH 里的 Python）")
    if force and not purge_vendor():
        mark("warn", ".vendor 删不干净（被占用了，比如别处正在跑 build.py），"
                     "改为在原目录上覆盖安装")
    if not pip_install(pip_extra):
        mark("fail", "pip 安装失败，看上面 pip 的报错")
        say("     国内网络慢可以换源：python install.py --index-url tuna")
        say("     没有网也没有包也能继续用，只是没有 AI 配音。")
        return 1

    say("[4/4] 验证")
    freed = strip_pycache()
    if freed:
        mark("ok", "清掉 __pycache__ 释放 %.1f MB" % (freed / 1048576.0))
    ok = verify()
    if ok:
        write_stamp()

    pkgs_now = vendor_packages()
    say("")
    say("=" * 68)
    if ok:
        mark("ok", "完成。.vendor 内 %d 个包，共 %.1f MB"
             % (len(pkgs_now), _du(VENDOR) / 1048576.0))
        say("     下一步：python doctor.py 看整体环境")
        return 0
    return 1


def _du(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        say("\n已中断")
        sys.exit(130)
