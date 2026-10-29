# -*- coding: utf-8 -*-
"""剪映草稿的加解密，直连 videoeditor.dll。

剪映 11.x 把 draft_content.json 这类文件整体加密：外层是 base64 文本，里面是
不去压缩的密文。官方没开放接口，但 DLL 里留着现成的导出函数：

    ?decrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU...@@AEBV34@0AEA_N@Z
    ?encrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU...@@AEBV34@@Z

这里用 ctypes 直接调，不需要编译 C++、不需要外部命令行工具、不写任何临时文件。
所有操作都在内存里完成。

依赖：本机装了对应版本的剪映专业版（dll 路径由 jycore.app_dir() 给出）。
"""
import ctypes
import os

DEFAULT_VERSION = "11.6.0.14552"

M_DECRYPT = ("?decrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@"
             "V?$allocator@D@2@@std@@AEBV34@0AEA_N@Z")
M_ENCRYPT = ("?encrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@"
             "V?$allocator@D@2@@std@@AEBV34@@Z")
M_ENABLE = "?enable@EncryptUtils@lvve@@QEAAX_N@Z"


class _CppStr(ctypes.Structure):
    # MSVC 的 std::string：16 字节 SSO 缓冲 + size + capacity
    _fields_ = [("buf", ctypes.c_char * 16),
                ("size", ctypes.c_size_t),
                ("cap", ctypes.c_size_t)]


def _to_cpp(data):
    s = _CppStr()
    n = len(data)
    if n < 16:
        if n:
            ctypes.memmove(ctypes.addressof(s), data, n)
        s.size, s.cap = n, 15
        return s, None
    holder = ctypes.create_string_buffer(data, n)
    s.size, s.cap = n, n
    ptr = ctypes.c_void_p(ctypes.addressof(holder))
    ctypes.memmove(ctypes.addressof(s), ctypes.byref(ptr), 8)
    return s, holder


def _from_cpp(s):
    n = s.size
    if n == 0:
        return b""
    if s.cap < 16:
        return bytes(s.buf[:n])
    raw = ctypes.string_at(ctypes.addressof(s), 8)
    ptr = ctypes.c_void_p.from_buffer_copy(raw).value
    return ctypes.string_at(ptr, n)


_state = {"app_dir": None, "loaded": None}


def set_app_dir(path):
    """换剪映版本时重新绑定 dll；传 None 则下次调用时自动定位。"""
    _state["app_dir"] = path
    _state["loaded"] = None


def app_dir():
    if _state["app_dir"]:
        return _state["app_dir"]
    import jycore
    _state["app_dir"] = jycore.app_dir()
    return _state["app_dir"]


def _load():
    if _state["loaded"] is not None:
        return _state["loaded"]
    d = app_dir()
    dll_path = os.path.join(d, "videoeditor.dll")
    if not os.path.exists(dll_path):
        raise RuntimeError("找不到 %s\n请确认装的是剪映专业版 %s。"
                           % (dll_path, DEFAULT_VERSION))
    os.add_dll_directory(d)
    dll = ctypes.WinDLL(dll_path, use_last_error=True)

    P5 = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.POINTER(ctypes.c_bool))
    P3 = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p)
    VE = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_bool)

    decrypt = P5(ctypes.cast(getattr(dll, M_DECRYPT), ctypes.c_void_p).value)
    encrypt = P3(ctypes.cast(getattr(dll, M_ENCRYPT), ctypes.c_void_p).value)
    enable = VE(ctypes.cast(getattr(dll, M_ENABLE), ctypes.c_void_p).value)
    _state["loaded"] = (dll, decrypt, encrypt, enable)
    return _state["loaded"]


def decrypt(encrypted):
    """密文 → 明文，返回 (bytes, 成功标志)。"""
    _, fn, _, _ = _load()
    src, _h1 = _to_cpp(encrypted)
    param, _h2 = _to_cpp(b"{}")
    out = _CppStr()
    out.size, out.cap = 0, 15
    ok = ctypes.c_bool(False)
    fn(None, ctypes.addressof(out), ctypes.addressof(src),
       ctypes.addressof(param), ctypes.byref(ok))
    return _from_cpp(out), ok.value


def encrypt(plain):
    """明文 → 密文。内部会先 enable(True)，否则 dll 原样返回。"""
    _, _, fn, enable = _load()
    enable(None, True)
    src, _h = _to_cpp(plain)
    out = _CppStr()
    out.size, out.cap = 0, 15
    fn(None, ctypes.addressof(out), ctypes.addressof(src))
    return _from_cpp(out)


def looks_plain(data):
    return data.lstrip()[:1] == b"{"
