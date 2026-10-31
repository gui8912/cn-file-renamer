# -*- coding: utf-8 -*-
"""
资源管理器右键菜单集成。

刻意写在 HKEY_CURRENT_USER 下（而不是 HKEY_CLASSES_ROOT）：
  - 不需要管理员权限，双击就能装
  - 换台电脑不需要提权，符合"拷过去就能用"的要求
  - 不影响系统级配置，卸载干净

注册位置：
  Directory\\shell            右键文件夹
  Directory\\Background\\shell 文件夹空白处右键
  *\\shell                     右键单个文件
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(BASE, 'launcher.py')

APP_NAME = '中文文件批量改名器'
KEY_DIR = r'Software\Classes\Directory\shell\RenameToolCN'
KEY_BG = r'Software\Classes\Directory\Background\shell\RenameToolCN'
KEY_FILE = r'Software\Classes\*\shell\RenameToolCN'

try:
    import winreg
    _HAS_WINREG = True
except ImportError:                                   # pragma: no cover
    winreg = None
    _HAS_WINREG = False


def is_windows():
    return os.name == 'nt' and _HAS_WINREG


def pythonw_path():
    """优先用 pythonw.exe，避免右键后弹出黑色控制台窗口。"""
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    for folder in (exe_dir, sys.prefix):
        for name in ('pythonw.exe', 'python.exe'):
            p = os.path.join(folder, name)
            if os.path.exists(p):
                return p
    return sys.executable


def command_line(target='%V', quote_target=True):
    arg = ('"%s"' % target) if quote_target else target
    return '"%s" "%s" %s' % (pythonw_path(), LAUNCHER, arg)


def _set_value(key_path, value_name, value):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, value_name, 0, winreg.REG_SZ, value)


def _delete_tree(root, path):
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as k:
            subs = []
            i = 0
            while True:
                try:
                    subs.append(winreg.EnumKey(k, i))
                    i += 1
                except OSError:
                    break
        for s in subs:
            _delete_tree(root, path + '\\' + s)
    except OSError:
        pass
    try:
        winreg.DeleteKey(root, path)
    except OSError:
        pass


def status():
    """返回当前安装状态与将要写入的命令。"""
    installed = False
    current_cmd = ''
    if is_windows():
        for key in (KEY_DIR, KEY_BG, KEY_FILE):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key + r'\command') as k:
                    installed = True
                    if key == KEY_DIR:
                        current_cmd = winreg.QueryValueEx(k, '')[0]
            except OSError:
                continue
    launcher_registered = bool(current_cmd and LAUNCHER.lower() in current_cmd.lower())
    return {
        'ok': True,
        'supported': is_windows(),
        'installed': installed,
        'needs_reinstall': bool(installed and current_cmd and not launcher_registered),
        'app': APP_NAME,
        'python': pythonw_path(),
        'launcher': LAUNCHER,
        'launcher_exists': os.path.exists(LAUNCHER),
    }


def install():
    if not is_windows():
        return {'ok': False, 'error': '仅支持 Windows'}
    if not os.path.exists(LAUNCHER):
        return {'ok': False, 'error': '缺少 launcher.py'}

    targets = [
        (KEY_DIR, '%V', True, 'Player'),   # 右键文件夹
        (KEY_BG, '%V', True, None),        # 文件夹空白处
        (KEY_FILE, '%*', False, 'Player'),  # 右键文件，支持多选
    ]
    try:
        for key, arg, quoted, multi in targets:
            _set_value(key, '', APP_NAME)
            _set_value(key, 'Icon', pythonw_path() + ',0')
            if multi:
                _set_value(key, 'MultiSelectModel', multi)
            _set_value(key + '\\command', '', command_line(arg, quoted))
    except OSError as e:
        return {'ok': False, 'error': '写入注册表失败：%s' % e}
    except PermissionError as e:
        return {'ok': False, 'error': '没有注册表写入权限：%s' % e}
    return {'ok': True, 'installed': True}


def uninstall():
    if not is_windows():
        return {'ok': False, 'error': '仅支持 Windows'}
    try:
        for key in (KEY_DIR, KEY_BG, KEY_FILE):
            _delete_tree(winreg.HKEY_CURRENT_USER, key)
    except OSError as e:
        return {'ok': False, 'error': '清理注册表失败：%s' % e}
    return {'ok': True, 'installed': False}
