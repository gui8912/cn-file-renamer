# -*- coding: utf-8 -*-
"""
本地路径桥（外部插件）——解决「浏览器拿不到真实路径」这个死限制。

浏览器出于安全，无论拖放还是网页里的目录选择框，都不向页面暴露文件的完整路径。
原 pick_folder 用 tkinter 的 askdirectory，底层是 Tk 自绘的 tk_chooseDirectory，
在中文 Windows 上常显示不出本地目录树。本插件完全绕开浏览器与 Tk：

  --pick      弹 Windows 原生文件夹对话框（SHBrowseForFolderW + BIF_NEWDIALOGSTYLE），
              把选中的真实路径打印到 stdout，供 app.py 的 /api/pick-folder 调用。
  --drop      常驻一个置顶小窗口，接收资源管理器的 WM_DROPFILES 拖放，
              拿到真实路径后直接 POST 给本地服务，页面会自动刷新（不新开标签页）。
  --selftest  不弹任何窗口的自检，用于 CI / 笔记本首次运行验证。

设计约束（与项目一致）：纯标准库、零 pip 依赖、Python 3.9+、只跑在本机 127.0.0.1。
所有 Win32 调用用 ctypes 完成，日志写 data/bridge.log（pythonw 下没有控制台）。

参考来源：Microsoft Learn「SHBrowseForFolderW / BROWSEINFOW / DragQueryFileW /
WM_DROPFILES」Win32 API 文档中的结构体与调用约定；此处为按文档独立实现的 ctypes
绑定，未复制任何第三方项目代码。
"""

import ctypes
import json
import os
import socket
import sys
import time
import urllib.parse
import urllib.request
from ctypes import wintypes

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, 'data', 'bridge.log')

# 64/32 位都要对；wintypes 里 WPARAM/LPARAM 的宽度在不同版本 Python 上不稳，自己定义。
_WPARAM = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t
_LRESULT = ctypes.c_ssize_t
_LPCWSTR = wintypes.LPCWSTR
# 句柄一律用 c_void_p：wintypes 在 3.9~3.12 间并非每个版本都定义
# HCURSOR / HBRUSH / HMODULE 等，笔记本可能是任意版本。
_HWND = ctypes.c_void_p
_HANDLE = ctypes.c_void_p
_ATOM = ctypes.c_uint16

IS_WIN = os.name == 'nt'


def log(msg):
    """pythonw 下 stdout 不可用，全部落文件。"""
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write('%s  %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


# ============================================================
# 与本地服务通信
# ============================================================

def _port_open(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.12)
            return s.connect_ex(('127.0.0.1', port)) == 0
    except OSError:
        return False


def find_server():
    """返回 (port, ping_dict)。

    比对 base 必须用 realpath：服务台用符号链接 apps\\rename-cn 启动服务，
    ping 里的 base 是链接路径，与项目真实路径字符串不相等但 realpath 相等。
    原 launcher.find_server 用 normcase 比较，会把正在跑的服务漏掉。
    """
    for port in range(8770, 8801):
        if not _port_open(port):
            continue
        try:
            with urllib.request.urlopen(
                    'http://127.0.0.1:%d/api/ping' % port, timeout=1.2) as r:
                data = json.loads(r.read().decode('utf-8'))
            if data.get('ok') and data.get('app') == 'rename-tool-cn':
                # 认准本项目启动的实例；服务台用符号链接启动时 ping.base 是链接路径，
                # 必须 realpath 才能与 BASE 对上（与 launcher.find_server 保持一致）。
                if os.path.normcase(os.path.realpath(data.get('base', ''))) == \
                   os.path.normcase(os.path.realpath(BASE)):
                    return port, data
        except Exception:
            continue
    return None, None


def _post(port, endpoint, body):
    req = urllib.request.Request(
        'http://127.0.0.1:%d%s' % (port, endpoint),
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode('utf-8'))


def normalize_paths(raw):
    """去重、去空、统一为绝对路径；目录与文件都保留。"""
    out, seen = [], set()
    for p in raw or []:
        if not isinstance(p, str) or not p.strip():
            continue
        ap = os.path.abspath(p.strip().strip('"'))
        key = os.path.normcase(ap)
        if key not in seen and os.path.exists(ap):
            seen.add(key)
            out.append(ap)
    return out


def push_paths(raw_paths):
    """把真实路径送进服务；返回 (ok, 提示文本)。

    顺序有依赖：先授权目录，再 add-paths（后者会校验白名单）。
    sid 从 ping 带回来，保证不触发页面的会话过期判定。
    bridge=True 让服务端递增 bridge_rev，页面轮询到就自动刷新列表。
    """
    paths = normalize_paths(raw_paths)
    if not paths:
        return False, '没有可用的路径'
    port, ping = find_server()
    if not port:
        return False, '没找到改名器服务，请先在服务台启动'
    # 旧版服务(<=1.2.2)的 /api/ping 没有 bridge_rev，也不会给 sid；
    # 硬 POST 会被会话校验拒掉，抛「清单已被另一个页面更新」这种误导性错误。
    # 直接前置说清楚：重启一次服务即可，别让用户去猜。
    if (ping or {}).get('bridge_rev') is None:
        return False, ('改名器服务还是旧版，请重启到 1.2.3 再拖放'
                       '（服务台：停止 → 启动；或重开 启动.bat）')

    # 授权：文件取其父目录，目录取自身。系统目录由服务端拒绝，这里不重复判断。
    auth_errors = []
    for p in paths:
        d = p if os.path.isdir(p) else os.path.dirname(p)
        try:
            r = _post(port, '/api/roots/add', {'path': d})
            if not r.get('ok'):
                auth_errors.append(r.get('error', '授权失败'))
        except Exception as e:
            auth_errors.append(str(e))

    try:
        res = _post(port, '/api/add-paths', {
            'paths': paths,
            'sid': (ping or {}).get('sid', ''),
            'recursive': False,
            'include_dirs': False,
            'bridge': True,
        })
    except Exception as e:
        return False, '提交失败：%s' % e
    if not res.get('ok'):
        return False, res.get('error', '提交失败')

    added = res.get('added', 0)
    errs = (res.get('errors') or [])[:2] + auth_errors[:1]
    msg = '已加入 %d 项' % added
    if errs:
        msg += '；' + ' / '.join(str(x) for x in errs)
    log('push_paths: %d -> added=%d  %s' % (len(paths), added, msg))
    return True, msg


# ============================================================
# Windows 原生文件夹对话框
# ============================================================

if IS_WIN:
    _user32 = ctypes.WinDLL('user32', use_last_error=True)
    _shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    _ole32 = ctypes.WinDLL('ole32', use_last_error=True)
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [
            ('hwndOwner', _HWND),
            ('pidlRoot', ctypes.c_void_p),
            ('pszDisplayName', wintypes.LPWSTR),
            ('lpszTitle', _LPCWSTR),
            ('ulFlags', wintypes.UINT),
            ('lpfn', ctypes.c_void_p),
            ('lParam', _LPARAM),
            ('iImage', ctypes.c_int),
        ]

    _shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    _shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    _shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    _shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    _shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                        wintypes.LPWSTR, wintypes.UINT]
    _shell32.DragQueryFileW.restype = wintypes.UINT
    _shell32.DragAcceptFiles.argtypes = [_HWND, wintypes.BOOL]
    _shell32.DragFinish.argtypes = [ctypes.c_void_p]
    _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]


def pick_native_folder():
    """弹原生目录树，返回真实路径；取消或非 Windows 返回 None。"""
    if not IS_WIN:
        return None
    # BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_NONEWFOLDERBUTTON
    # NEWDIALOGSTYLE 才有「此电脑 / 各盘符」的完整树和可调整大小的现代界面，
    # 这正是 Tk 自绘目录框缺的部分。
    flags = 0x0001 | 0x0040 | 0x0200
    _ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    try:
        disp = ctypes.create_unicode_buffer(260)
        bi = BROWSEINFOW()
        bi.hwndOwner = None
        bi.pidlRoot = None
        bi.pszDisplayName = ctypes.cast(disp, wintypes.LPWSTR)
        bi.lpszTitle = '选择要批量改名的文件夹'
        bi.ulFlags = flags
        bi.lpfn = None
        bi.lParam = 0
        bi.iImage = 0
        pidl = _shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            ok = _shell32.SHGetPathFromIDListW(pidl, buf)
            return buf.value if ok and buf.value else None
        finally:
            _ole32.CoTaskMemFree(pidl)
    finally:
        _ole32.CoUninitialize()


# ============================================================
# 本地拖放接收窗口
# ============================================================

if IS_WIN:
    _WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, _HWND, wintypes.UINT,
                                  _WPARAM, _LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ('style', wintypes.UINT),
            ('lpfnWndProc', _WNDPROC),
            ('cbClsExtra', ctypes.c_int),
            ('cbWndExtra', ctypes.c_int),
            ('hInstance', _HANDLE),
            ('hIcon', _HANDLE),
            ('hCursor', _HANDLE),
            ('hbrBackground', _HANDLE),
            ('lpszMenuName', _LPCWSTR),
            ('lpszClassName', _LPCWSTR),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]

    class MSG(ctypes.Structure):
        _fields_ = [
            ('hwnd', _HWND), ('message', wintypes.UINT),
            ('wParam', _WPARAM), ('lParam', _LPARAM),
            ('time', wintypes.DWORD), ('pt', POINT),
        ]

    _user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _user32.RegisterClassW.restype = _ATOM
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, _LPCWSTR, _LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        _HWND, _HANDLE, _HANDLE, ctypes.c_void_p]
    _user32.CreateWindowExW.restype = _HWND
    _user32.DefWindowProcW.argtypes = [_HWND, wintypes.UINT, _WPARAM, _LPARAM]
    _user32.DefWindowProcW.restype = _LRESULT
    _user32.SetWindowTextW.argtypes = [_HWND, _LPCWSTR]
    _user32.SetWindowTextW.restype = wintypes.BOOL
    _user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), _HWND,
                                    wintypes.UINT, wintypes.UINT]
    _user32.GetMessageW.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.LoadCursorW.argtypes = [_HANDLE, _LPCWSTR]
    _user32.LoadCursorW.restype = _HANDLE
    _kernel32.GetModuleHandleW.argtypes = [_LPCWSTR]
    _kernel32.GetModuleHandleW.restype = _HANDLE

    WM_DESTROY = 0x0002
    WM_DROPFILES = 0x0233
    WS_EX_TOPMOST = 0x00000008
    WS_OVERLAPPEDWINDOW = 0x00CF0000
    WS_VISIBLE = 0x10000000
    WS_CHILD = 0x40000000
    SS_CENTER = 0x00000001

    _status_hwnd = [None]
    _wndproc_keepalive = [None]  # WNDPROC 回调被 GC 会让窗口过程地址失效


def _drop_paths(hdrop):
    """从 HDROP 读出所有真实路径（Unicode 版，中文文件名无损）。"""
    paths = []
    try:
        hd = ctypes.c_void_p(hdrop)
        count = _shell32.DragQueryFileW(hd, 0xFFFFFFFF, None, 0)
        for i in range(count):
            n = _shell32.DragQueryFileW(hd, i, None, 0)
            buf = ctypes.create_unicode_buffer(n + 1)
            _shell32.DragQueryFileW(hd, i, buf, n + 1)
            paths.append(buf.value)
    finally:
        _shell32.DragFinish(ctypes.c_void_p(hdrop))
    return paths


def drop_window():
    """常驻置顶小窗口：把资源管理器里的文件/文件夹拖进来即可。"""
    if not IS_WIN:
        log('drop_window 仅支持 Windows')
        return 1

    def wnd_proc(hwnd, msg, wparam, lparam):
        if msg == WM_DROPFILES:
            raw = _drop_paths(wparam)
            ok, text = push_paths(raw)
            log('drop: %d 项 -> %s %s' % (len(raw), ok, text))
            if _status_hwnd[0]:
                _user32.SetWindowTextW(_status_hwnd[0], text)
            return 0
        if msg == WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    cb = _WNDPROC(wnd_proc)
    _wndproc_keepalive[0] = cb
    hinst = _kernel32.GetModuleHandleW(None)
    cls_name = 'RenameToolBridgeWnd'
    wc = WNDCLASSW()
    wc.style = 0
    wc.lpfnWndProc = cb
    wc.cbClsExtra = wc.cbWndExtra = 0
    wc.hInstance = hinst
    wc.hIcon = None
    wc.hCursor = _user32.LoadCursorW(
        None, ctypes.cast(ctypes.c_void_p(32512), _LPCWSTR))  # IDC_ARROW
    wc.hbrBackground = ctypes.c_void_p(16)  # COLOR_BTNFACE + 1
    wc.lpszMenuName = None
    wc.lpszClassName = cls_name
    if not _user32.RegisterClassW(ctypes.byref(wc)):
        log('RegisterClassW 失败: %s' % ctypes.get_last_error())
        return 1

    hwnd = _user32.CreateWindowExW(
        WS_EX_TOPMOST, cls_name, '改名器 · 拖放助手',
        WS_OVERLAPPEDWINDOW | WS_VISIBLE,
        180, 160, 460, 190, None, None, hinst, None)
    if not hwnd:
        log('CreateWindowExW 失败: %s' % ctypes.get_last_error())
        return 1

    _status_hwnd[0] = _user32.CreateWindowExW(
        0, 'Static',
        '把文件或文件夹从资源管理器拖到这里\n'
        '（一次可拖多个；改完关掉本窗口即可）',
        WS_CHILD | WS_VISIBLE | SS_CENTER,
        16, 20, 412, 110, hwnd, None, hinst, None)
    _shell32.DragAcceptFiles(hwnd, True)
    log('拖放助手窗口已启动')

    msg = MSG()
    while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        _user32.TranslateMessage(ctypes.byref(msg))
        _user32.DispatchMessageW(ctypes.byref(msg))
    return 0


# ============================================================
# CLI
# ============================================================

def selftest():
    """无 GUI 自检：API 绑定、结构体、服务可达性、纯逻辑。"""
    out = []
    out.append('platform: %s' % ('Windows' if IS_WIN else 'non-Windows'))
    if IS_WIN:
        out.append('BROWSEINFOW size: %d' % ctypes.sizeof(BROWSEINFOW))
        out.append('SHBrowseForFolderW: %s' %
                   ('OK' if getattr(_shell32, 'SHBrowseForFolderW', None) else 'MISSING'))
        out.append('DragQueryFileW: %s' %
                   ('OK' if getattr(_shell32, 'DragQueryFileW', None) else 'MISSING'))
        out.append('RegisterClassW: %s' %
                   ('OK' if getattr(_user32, 'RegisterClassW', None) else 'MISSING'))
    p, ping = find_server()
    out.append('server: %s' % (('port %d, bridge_rev=%s' %
                                (p, ping.get('bridge_rev'))) if p else 'not running'))
    out.append('normalize_paths: %r' % normalize_paths(['', '  ', __file__, __file__]))
    print('\n'.join(out))
    return 0


def main():
    argv = sys.argv[1:]
    if '--selftest' in argv:
        return selftest()
    if '--pick' in argv:
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
        path = pick_native_folder()
        # stdout 只允许出现一行路径：服务端按最后一行解析，日志一律写文件。
        print(path or '')
        log('pick -> %r' % path)
        return 0
    if '--drop' in argv:
        return drop_window()
    print(__doc__ or 'usage: folder_bridge.py --pick | --drop | --selftest')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log('异常: %s: %s' % (type(e).__name__, e))
        sys.exit(1)
