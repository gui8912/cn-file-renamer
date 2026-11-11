# -*- coding: utf-8 -*-
"""
本地路径桥（外部插件）——解决「浏览器拿不到真实路径」这个死限制。

浏览器出于安全，无论拖放还是网页里的目录选择框，都不向页面暴露文件的完整路径。
原 pick_folder 用 tkinter 的 askdirectory，底层是 Tk 自绘的 tk_chooseDirectory，
在中文 Windows 上常显示不出本地目录树。本插件完全绕开浏览器与 Tk：

  --pick      弹 Windows 现代文件夹对话框（IFileOpenDialog + FOS_PICKFOLDERS，
              带地址栏/搜索/文件列表），默认打开上次选过的目录；把真实路径打印到
              stdout，供 app.py 的 /api/pick-folder 调用。COM 不可用时回退老式框。
  --drop      常驻一个置顶小窗口：既能接收资源管理器的 WM_DROPFILES 拖放，也有
              「选择文件…（可多选）」「选择文件夹…」两个按钮，点一下同样弹现代对话框。
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
import threading  # modified: Show 阻塞期间用守护线程把对话框置顶
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

# modified: 对话框自动置顶。HWND_TOPMOST = (HWND)-1；只改 z-order，不动位置/大小。
_HWND_TOPMOST = ctypes.c_void_p(-1)  # modified
_SWP_NOSIZE = 0x0001  # modified
_SWP_NOMOVE = 0x0002  # modified
_SWP_NOACTIVATE = 0x0010  # modified: 重复置顶时不能反复抢焦点
_DLG_TITLE_FOLDER = '选择要批量改名的文件夹'  # modified
_DLG_TITLE_FILES = '选择要加入改名器的文件（可多选）'  # modified
_DLG_TITLE_LEGACY = '浏览文件夹'  # modified


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

    # modified: 置顶所需的 user32 绑定
    _user32.FindWindowW.argtypes = [_LPCWSTR, _LPCWSTR]  # modified
    _user32.FindWindowW.restype = _HWND  # modified
    _user32.SetWindowPos.argtypes = [  # modified
        _HWND, _HWND, ctypes.c_int, ctypes.c_int,  # modified
        ctypes.c_int, ctypes.c_int, wintypes.UINT]  # modified
    _user32.SetWindowPos.restype = wintypes.BOOL  # modified
    _user32.SetForegroundWindow.argtypes = [_HWND]  # modified
    _user32.SetForegroundWindow.restype = wintypes.BOOL  # modified


# ---- 现代文件/文件夹对话框：IFileOpenDialog（Vista+） ----
# SHBrowseForFolderW 是 Win98 时代的老树形框，没有地址栏、不能搜索、不能看文件列表。
# 这里手写 COM vtable 调用（纯 ctypes，无 comtypes/pywin32 依赖），按
# Microsoft Learn 的 IFileDialog / IFileOpenDialog / IShellItem 接口顺序索引。
# 若新旧接口都不可用，仍回退到老对话框，保证「至少能选」。
_CLSID_FILE_OPEN_DIALOG = '{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}'
_IID_IFILE_OPEN_DIALOG = '{D57C7288-D4AD-4768-BE02-9D969532D960}'
_IID_ISHELLITEM = '{43826D1E-E718-42EE-BC55-A1E261C37BFE}'

_FOS_NOCHANGEDIR = 0x00000008
_FOS_PICKFOLDERS = 0x00000020
_FOS_FORCEFILESYSTEM = 0x00000040
_FOS_ALLOWMULTISELECT = 0x00000200
_FOS_PATHMUSTEXIST = 0x00000800
_FOS_FILEMUSTEXIST = 0x00001000
_SIGDN_FILESYSPATH = 0x80058000
_CLSCTX_ALL = 0x17

# vtable 槽位：IUnknown(0..2) / IModalWindow(3) / IFileDialog(4..26) /
# IFileOpenDialog(27..28) / IShellItem(3..7) / IShellItemArray(3..9)
_VT_RELEASE = 2
_VT_SHOW = 3
_VT_SETOPTIONS = 9
_VT_SETFOLDER = 12
_VT_SETTITLE = 17
_VT_GETRESULT = 20
_VT_GETRESULTS = 27
_VT_ITEM_DISPLAYNAME = 5
_VT_ARRAY_COUNT = 7
_VT_ARRAY_ITEMAT = 8

STATE_FILE = os.path.join(BASE, 'data', 'bridge_state.json')  # 记住上次目录


def load_last_folder():
    """上次成功选择的目录；目录已被删掉就忽略，别让对话框定位到无效路径。"""
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            p = (json.load(f).get('last_folder') or '').strip()
        return p if p and os.path.isdir(p) else ''
    except Exception:
        return ''


def save_last_folder(path):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'last_folder': path}, f, ensure_ascii=False)
    except Exception as e:
        log('保存上次目录失败: %s' % e)


if IS_WIN:
    class GUID(ctypes.Structure):
        _fields_ = [('Data1', ctypes.c_ulong), ('Data2', ctypes.c_ushort),
                    ('Data3', ctypes.c_ushort), ('Data4', ctypes.c_ubyte * 8)]

    _ole32.CLSIDFromString.argtypes = [_LPCWSTR, ctypes.POINTER(GUID)]
    _ole32.CLSIDFromString.restype = ctypes.c_long
    _ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.POINTER(GUID),
                                        ctypes.POINTER(ctypes.c_void_p)]
    _ole32.CoCreateInstance.restype = ctypes.c_long
    _shell32.SHCreateItemFromParsingName.argtypes = [
        _LPCWSTR, ctypes.c_void_p, ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p)]
    _shell32.SHCreateItemFromParsingName.restype = ctypes.c_long


def _guid(text):
    g = GUID()
    if _ole32.CLSIDFromString(text, ctypes.byref(g)) != 0:
        raise OSError('CLSIDFromString 失败: %s' % text)
    return g


def _vcall(obj, index, argtypes, *args):
    """调用 COM 对象 vtable 第 index 个方法，返回 HRESULT(c_long)。"""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])(obj, *args)


def _shell_item_from_path(path):
    iid = _guid(_IID_ISHELLITEM)
    item = ctypes.c_void_p()
    hr = _shell32.SHCreateItemFromParsingName(
        path, None, ctypes.byref(iid), ctypes.byref(item))
    return item if hr == 0 and item else None


def _shell_item_path(item):
    """取 IShellItem 的文件系统路径，并负责释放字符串和对象。"""
    disp = ctypes.c_void_p()
    try:
        hr = _vcall(item, _VT_ITEM_DISPLAYNAME,
                    [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
                    _SIGDN_FILESYSPATH, ctypes.byref(disp))
        return (ctypes.wstring_at(disp) or '') if hr == 0 and disp else ''
    finally:
        if disp:
            _ole32.CoTaskMemFree(disp)
        _vcall(item, _VT_RELEASE, [])


def _pop_to_front(title, stop):
    """守护线程：Show 阻塞期间把刚出现的对话框置顶。

    网页点「浏览」时对话框由后台子进程弹出，没有前台 owner，很容易被浏览器
    压在下面。按「类名 #32770 + 标题」精确匹配后设 HWND_TOPMOST。

    注意不能只设一次：实弹验证发现文件夹模式下 IFileDialog 在完全显示时
    会自己重设 z-order，一次性置顶会被覆盖掉。所以在 Show 返回前持续压住
    （每 0.12s 一次）；对话框关闭后 FindWindowW 返回空、不再操作，
    窗口销毁时 TOPMOST 状态随之消失，不会留下残留。
    """
    if not IS_WIN:
        return False
    done = False
    while not stop.is_set():
        hwnd = _user32.FindWindowW('#32770', title)
        if hwnd:
            _user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                                 _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
            if not done:  # 只抢一次焦点，别打断用户在对话框里输入
                _user32.SetForegroundWindow(hwnd)
                log('对话框已置顶: %s' % title)
                done = True
        time.sleep(0.12)
    return done


def _start_topmost(title):
    """起守护线程，返回 stop_event；调用方在 Show 返回后 set()。"""
    stop = threading.Event()
    threading.Thread(target=_pop_to_front, args=(title, stop),
                     daemon=True).start()
    return stop


def _modern_dialog(folders, hwnd_owner=None):
    """弹现代对话框。返回路径列表；[] = 用户取消；None = 接口不可用需回退。"""
    if not IS_WIN:
        return None
    hr = _ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    if hr not in (0, 1):                    # 1 = S_FALSE（本线程已初始化）
        return None
    dlg = ctypes.c_void_p()
    try:
        clsid = _guid(_CLSID_FILE_OPEN_DIALOG)
        iid = _guid(_IID_IFILE_OPEN_DIALOG)
        hr = _ole32.CoCreateInstance(ctypes.byref(clsid), None, _CLSCTX_ALL,
                                     ctypes.byref(iid), ctypes.byref(dlg))
        if hr != 0 or not dlg:
            log('IFileOpenDialog 创建失败 hr=%s' % hr)
            return None

        opts = _FOS_FORCEFILESYSTEM | _FOS_PATHMUSTEXIST | _FOS_NOCHANGEDIR
        if folders:
            opts |= _FOS_PICKFOLDERS
        else:
            opts |= _FOS_ALLOWMULTISELECT | _FOS_FILEMUSTEXIST
        _vcall(dlg, _VT_SETOPTIONS, [wintypes.DWORD], opts)
        title = _DLG_TITLE_FOLDER if folders else _DLG_TITLE_FILES  # modified
        _vcall(dlg, _VT_SETTITLE, [_LPCWSTR], title)  # modified

        # 默认打开上次选过的目录；不存在时 load_last_folder 会返回空串。
        last = load_last_folder()
        if last:
            item = _shell_item_from_path(last)
            if item:
                _vcall(dlg, _VT_SETFOLDER, [ctypes.c_void_p], item)
                _vcall(item, _VT_RELEASE, [])

        stop = _start_topmost(title)  # modified: 弹出即自动置顶
        try:  # modified
            shown = _vcall(dlg, _VT_SHOW, [_HWND], hwnd_owner)  # modified
        finally:  # modified
            stop.set()  # modified
        if shown != 0:  # modified
            return []                       # 取消（ERROR_CANCELLED）

        if folders:
            item = ctypes.c_void_p()
            if _vcall(dlg, _VT_GETRESULT,
                      [ctypes.POINTER(ctypes.c_void_p)],
                      ctypes.byref(item)) != 0 or not item:
                return []
            out = [_shell_item_path(item)]
        else:
            arr = ctypes.c_void_p()
            if _vcall(dlg, _VT_GETRESULTS,
                      [ctypes.POINTER(ctypes.c_void_p)],
                      ctypes.byref(arr)) != 0 or not arr:
                return []
            try:
                n = wintypes.DWORD()
                _vcall(arr, _VT_ARRAY_COUNT,
                       [ctypes.POINTER(wintypes.DWORD)], ctypes.byref(n))
                out = []
                for i in range(n.value):
                    item = ctypes.c_void_p()
                    if _vcall(arr, _VT_ARRAY_ITEMAT,
                              [wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)],
                              i, ctypes.byref(item)) == 0 and item:
                        out.append(_shell_item_path(item))
            finally:
                _vcall(arr, _VT_RELEASE, [])

        out = [p for p in out if p]
        if out:
            first = out[0]
            save_last_folder(first if os.path.isdir(first)
                             else os.path.dirname(first))
        return out
    except Exception as e:
        log('IFileOpenDialog 异常: %s' % e)
        return None
    finally:
        if dlg:
            _vcall(dlg, _VT_RELEASE, [])
        _ole32.CoUninitialize()


def _legacy_pick_folder():
    """老式 SHBrowseForFolderW，仅在 IFileOpenDialog 不可用时兜底。"""
    if not IS_WIN:
        return None
    flags = 0x0001 | 0x0040 | 0x0200
    _ole32.CoInitializeEx(None, 0x2)
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
        stop = _start_topmost(_DLG_TITLE_LEGACY)  # modified: 回退框同样置顶
        try:  # modified
            pidl = _shell32.SHBrowseForFolderW(ctypes.byref(bi))  # modified
        finally:  # modified
            stop.set()  # modified
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


def pick_native_folder(hwnd_owner=None):
    """现代文件夹对话框，返回真实路径；取消返回 None。"""
    if not IS_WIN:
        return None
    res = _modern_dialog(True, hwnd_owner)
    if res is None:
        log('现代对话框不可用，回退 SHBrowseForFolderW')
        return _legacy_pick_folder()
    if res:
        save_last_folder(res[0])
    return res[0] if res else None


def pick_native_files(hwnd_owner=None):
    """现代文件对话框（可多选），返回真实路径列表。"""
    if not IS_WIN:
        return []
    res = _modern_dialog(False, hwnd_owner)
    return res or []


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
    WM_COMMAND = 0x0111  # modified: 按钮点击
    IDC_BTN_FILES = 1001  # modified
    IDC_BTN_FOLDER = 1002  # modified
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
        if msg == WM_COMMAND:  # modified: 点按钮弹出现代对话框，选完直接加入
            cid = wparam & 0xFFFF  # modified
            if cid == IDC_BTN_FILES:  # modified
                paths, tag = pick_native_files(hwnd), '文件'  # modified
            elif cid == IDC_BTN_FOLDER:  # modified
                d = pick_native_folder(hwnd)  # modified
                paths, tag = ([d] if d else []), '文件夹'  # modified
            else:  # modified
                return 0  # modified
            if not paths:  # modified: 用户取消，不弹第二个框、不报错
                return 0  # modified
            ok, text = push_paths(paths)  # modified
            log('按钮选%s: %d 项 -> %s %s' % (tag, len(paths), ok, text))  # modified
            if _status_hwnd[0]:  # modified
                _user32.SetWindowTextW(_status_hwnd[0], text)  # modified
            return 0  # modified
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
        180, 160, 470, 250, None, None, hinst, None)
    if not hwnd:
        log('CreateWindowExW 失败: %s' % ctypes.get_last_error())
        return 1

    # modified: 加两个按钮，点一下就弹现代对话框；拖放区仍保留。
    _user32.CreateWindowExW(  # modified
        0, 'Static', '把文件 / 文件夹从资源管理器拖进窗口任意位置',  # modified
        WS_CHILD | WS_VISIBLE | SS_CENTER,  # modified
        16, 14, 422, 32, hwnd, None, hinst, None)  # modified
    btn_files = _user32.CreateWindowExW(  # modified
        0, 'Button', '选择文件…（可多选）',  # modified
        WS_CHILD | WS_VISIBLE,  # modified
        24, 58, 196, 34, hwnd,  # modified
        ctypes.c_void_p(IDC_BTN_FILES), hinst, None)  # modified
    btn_folder = _user32.CreateWindowExW(  # modified
        0, 'Button', '选择文件夹…',  # modified
        WS_CHILD | WS_VISIBLE,  # modified
        240, 58, 196, 34, hwnd,  # modified
        ctypes.c_void_p(IDC_BTN_FOLDER), hinst, None)  # modified
    log('按钮创建结果: files=%s folder=%s err=%s' %  # modified
        (btn_files, btn_folder, ctypes.get_last_error()))  # modified
    _status_hwnd[0] = _user32.CreateWindowExW(  # modified
        0, 'Static',  # modified
        '也可以点上面按钮直接选，选完自动加入改名器列表\n'  # modified
        '（下次打开会默认停在本次选过的文件夹）',  # modified
        WS_CHILD | WS_VISIBLE | SS_CENTER,  # modified
        16, 106, 422, 76, hwnd, None, hinst, None)  # modified
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
        out.append('topmost(SetWindowPos): %s' %  # modified
                   ('OK' if getattr(_user32, 'SetWindowPos', None) else 'MISSING'))  # modified
        # 只创建 COM 对象、不 Show，验证现代对话框接口在本机可用。
        _ole32.CoInitializeEx(None, 0x2)  # modified
        dlg = ctypes.c_void_p()  # modified
        try:  # modified
            clsid = _guid(_CLSID_FILE_OPEN_DIALOG)  # modified
            iid = _guid(_IID_IFILE_OPEN_DIALOG)  # modified
            r = _ole32.CoCreateInstance(ctypes.byref(clsid), None, _CLSCTX_ALL,  # modified
                                        ctypes.byref(iid), ctypes.byref(dlg))  # modified
            out.append('IFileOpenDialog: %s (hr=%s)' %  # modified
                       ('OK' if r == 0 and dlg else 'FAIL', r))  # modified
        except Exception as e:  # modified
            out.append('IFileOpenDialog: FAIL %s' % e)  # modified
        finally:  # modified
            if dlg:  # modified
                _vcall(dlg, _VT_RELEASE, [])  # modified
            _ole32.CoUninitialize()  # modified
        out.append('last_folder: %r' % load_last_folder())  # modified
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
