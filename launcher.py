# -*- coding: utf-8 -*-
"""
右键菜单入口。由资源管理器调用，收到一个路径（文件或目录）。

干了三件事：
1. 确保本地服务在跑（没跑就后台拉起来）
2. 把该目录自动加入白名单（省得每次手工授权）
3. 打开浏览器并带上路径，自动扫描

用 pythonw.exe 调用时没有控制台窗口，所以任何异常都写进 data/launcher.log。
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

LOG = os.path.join(BASE, 'data', 'launcher.log')


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write('%s  %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


def _port_open(port):
    """先用 socket 探活，避免对每个关闭端口都等一遍 HTTP 超时。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.12)
            return s.connect_ex(('127.0.0.1', port)) == 0
    except OSError:
        return False


def find_server():
    for port in range(8770, 8801):
        if not _port_open(port):
            continue
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/api/ping' % port, timeout=1.5) as r:
                data = json.loads(r.read().decode('utf-8'))
                if not data.get('ok') or data.get('app') != 'rename-tool-cn':
                    continue
                # 多副本并存时必须认准本目录启动的实例，否则授权写进 A、页面却打开 B。
                if os.path.normcase(data.get('base', '')) == os.path.normcase(BASE):
                    return port
        except Exception:
            continue
    return None


def start_server():
    py = os.path.join(sys.prefix, 'python.exe')
    if not os.path.exists(py):
        py = sys.executable
    flags = 0
    if os.name == 'nt':
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | \
                getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    env = dict(os.environ)
    env['RENAME_NO_BROWSER'] = '1'           # 我们自己开浏览器，别让它重复开
    try:
        subprocess.Popen([py, os.path.join(BASE, 'app.py')],
                         cwd=BASE, env=env, creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log('启动服务失败: %s' % e)
        return None
    for _ in range(40):                       # 最多等 20 秒
        time.sleep(0.5)
        p = find_server()
        if p:
            return p
    return None


def authorize(path):
    """把目录写进白名单。服务每次都读文件，所以改文件即刻生效。"""
    try:
        from core import engine
        cfg = engine.load_config()
        real = os.path.abspath(path)
        if engine.is_path_blocked(real):
            log('拒绝自动授权系统目录: %s' % real)
            return
        if not any(os.path.normcase(r) == os.path.normcase(real) for r in cfg['trusted_roots']):
            cfg['trusted_roots'].append(real)
            engine.save_config(cfg)
            log('已自动授权: %s' % real)
    except Exception as e:
        log('授权失败: %s' % e)


def main():
    targets = []
    for a in sys.argv[1:]:
        a = a.strip().strip('"')
        if a:
            targets.append(os.path.abspath(a))
    log('调用，参数=%r' % targets)

    files, dirs = [], []
    for target in targets:
        if os.path.isdir(target):
            authorize(target)
            dirs.append(target)
        elif os.path.isfile(target):
            authorize(os.path.dirname(target))
            files.append(target)

    port = find_server()
    if not port:
        log('服务未运行，正在启动…')
        port = start_server()
    if not port:
        log('服务启动失败')
        return 1

    import urllib.parse
    url = 'http://127.0.0.1:%d/' % port
    if files and not dirs:
        url += '?' + '&'.join('path=' + urllib.parse.quote(p) for p in files)
    elif dirs:
        # 多选目录仍先处理第一个；其余目录已授权，可在页面切换。
        url += '?root=' + urllib.parse.quote(dirs[0]) + '&auto=1'
        if len(dirs) > 1:
            log('多选目录，仅自动扫描第一个：%s' % dirs[0])
    log('目标地址 %s' % url)
    if os.environ.get('RENAME_LAUNCHER_NO_OPEN'):
        log('测试模式：不打开浏览器')
        return 0
    try:
        webbrowser.open(url)
    except Exception as e:
        log('打开浏览器失败: %s' % e)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log('异常: %s: %s' % (type(e).__name__, e))
        sys.exit(1)
