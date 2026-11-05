# -*- coding: utf-8 -*-
"""
中文文件批量改名器 —— 本地服务入口

零依赖：只用 Python 标准库，不需要 pip install 任何东西。
启动：双击「启动.bat」或执行  python app.py
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from core import __version__ as VERSION  # noqa: E402
from core import engine, llm, rules, shell_ext, text_norm, validate   # noqa: E402

WEB = os.path.join(BASE, 'web')
HOST = '127.0.0.1'

# 单用户、单活动清单；sid 防止两个标签页交替扫描后把 A 的规则套到 B 上。
_SESSION = {'root': '', 'items': [], 'recursive': False, 'focus': None, 'sid': ''}
_LOCK = threading.Lock()
_INSTANCE_LOCK = None

MIME = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.ico': 'image/x-icon',
}


def _session_stale(payload):
    """请求携带的 sid 与服务端当前清单不一致时拒绝，避免跨标签页误改。"""
    sid = payload.get('sid') if isinstance(payload, dict) else None
    current = _SESSION.get('sid')
    if not current:
        return False
    if isinstance(payload, dict) and payload.get('new_session'):
        return False
    # 缺 sid / 空 sid 一律视为过期；否则旧客户端可绕过并污染另一个标签页。
    return sid != current


class Api:
    """业务处理，与 HTTP 解耦，方便单独测试。"""

    # ---------- 配置 ----------
    @staticmethod
    def state():
        cfg = engine.load_config()
        lcfg = llm.load_conf()
        return {
            'ok': True,
            'trusted_roots': cfg['trusted_roots'],
            'max_scan': cfg['max_scan'],
            'lower_ext': cfg['lower_ext'],
            'rules': rules.rule_catalog(),
            'default_enabled': rules.default_enabled(),
            'default_params': rules.default_params(),
            'common_dirs': _common_dirs(),
            'llm': {
                'ready': llm.is_ready(lcfg),
                'enabled': lcfg['enabled'],
                'base_url': lcfg['base_url'],
                'model': lcfg['model'],
                'has_key': bool(lcfg.get('api_key')),
                'prompt': lcfg['prompt'],
            },
            'version': VERSION,
        }

    # ---------- 规则方案 ----------
    @staticmethod
    def presets_get():
        return {'ok': True, 'presets': _preset_list(_load_presets())}

    @staticmethod
    def presets_save(payload):
        name = (payload.get('name') or '').strip()
        if not name:
            return {'ok': False, 'error': '方案名不能为空'}
        if len(name) > 50:
            return {'ok': False, 'error': '方案名不能超过 50 字'}
        presets = _load_presets()
        if payload.get('delete'):
            presets.pop(name, None)
        else:
            enabled = payload.get('enabled', {})
            params = payload.get('params', {})
            if not isinstance(enabled, dict) or not isinstance(params, dict):
                return {'ok': False, 'error': 'enabled/params 必须是对象'}
            sort_by = payload.get('sort_by') or 'name'
            target = payload.get('target') or 'name'
            if sort_by not in ('name', 'mtime', 'size', 'type', 'none'):
                return {'ok': False, 'error': '排序方式无效'}
            if target not in ('name', 'ext', 'both'):
                return {'ok': False, 'error': '改名目标无效'}
            if name not in presets and len(presets) >= 100:
                return {'ok': False, 'error': '方案最多保存 100 个'}
            presets[name] = {
                # 不用 `or {}`：显式传入空映射也是有意义的（全部规则关闭）。
                'enabled': enabled,
                'params': params,
                'lower_ext': bool(payload.get('lower_ext')),
                'auto_dedupe': bool(payload.get('auto_dedupe')),
                'sort_by': sort_by,
                'sort_desc': bool(payload.get('sort_desc')),
                'target': target,
            }
        _save_presets(presets)
        return {'ok': True, 'presets': _preset_list(presets), 'name': name}

    @staticmethod
    def presets_delete(payload):
        payload = dict(payload or {})
        payload['delete'] = True
        return Api.presets_save(payload)

    # ---------- 噪声词库 ----------
    @staticmethod
    def noise_get():
        return {'ok': True, 'words': rules.load_noise_words(),
                'default': rules.DEFAULT_NOISE}

    @staticmethod
    def noise_save(payload):
        risky = []
        if payload.get('restore'):
            words = list(rules.DEFAULT_NOISE)
        else:
            text = payload.get('text') or ''
            if not isinstance(text, str):
                return {'ok': False, 'error': 'text 必须是字符串'}
            words = [w.strip() for w in re.split(r'[\s,，、;；]+', text) if w.strip()]
            # 单字词极易误伤（比如加个「的」），保存时在界面上提醒
            risky = [w for w in words if len(w) == 1]
        saved = rules.save_noise_words(words)
        return {'ok': True, 'words': saved, 'count': len(saved), 'risky': risky}

    # ---------- 待处理清单 ----------
    @staticmethod
    def add_paths(payload):
        """按完整路径加入清单（配合「复制为路径」粘贴使用）。"""
        if _session_stale(payload):
            return {'ok': False, 'error': '清单已被另一个页面更新，请重新扫描', 'stale': True}
        paths = payload.get('paths') or []
        if not isinstance(paths, list):
            return {'ok': False, 'error': 'paths 必须是数组'}
        if payload.get('new_session'):
            # 右键打开新标签页是显式的新会话：旧标签页随后会因 sid 不同被拒绝。
            with _LOCK:
                _SESSION['items'] = []
                _SESSION['focus'] = None
                _SESSION['root'] = ''
                _SESSION['sid'] = uuid.uuid4().hex[:12]
        added, errors, keys = [], [], set()
        first_dir = ''

        def _key(p):
            return os.path.normcase(os.path.abspath(p))

        with _LOCK:
            items = _SESSION['items']
            have = {_key(i['path']) for i in items}

            for raw in paths:
                if not isinstance(raw, str):
                    errors.append('路径必须是字符串')
                    continue
                p = raw.strip().strip('"').strip("'")
                if not p:
                    continue
                p = os.path.abspath(p)
                if not os.path.exists(p):
                    errors.append('%s：路径不存在' % os.path.basename(p))
                    continue

                if os.path.isdir(p):
                    first_dir = first_dir or p
                    ok, why = engine.check_root_allowed(p)
                    if not ok:
                        errors.append('%s：%s' % (os.path.basename(p), why))
                        continue
                    sub, _ = engine.scan(p, recursive=bool(payload.get('recursive')),
                                         include_dirs=bool(payload.get('include_dirs')))
                    if not sub:
                        errors.append('%s：目录是空的' % os.path.basename(p))
                    for s in sub:
                        k = _key(s['path'])
                        if k not in have:
                            have.add(k)
                            items.append(s)
                            added.append(s['name'])
                        keys.add(k)
                else:
                    d = os.path.dirname(p)
                    first_dir = first_dir or d
                    ok, why = engine.check_root_allowed(d)
                    if not ok:
                        errors.append('%s：%s' % (os.path.basename(p), why))
                        continue
                    try:
                        st = os.stat(p)
                    except OSError:
                        errors.append('%s：读不到文件' % os.path.basename(p))
                        continue
                    k = _key(p)
                    if k not in have:
                        have.add(k)
                        items.append({
                            'name': os.path.basename(p), 'dir': d, 'path': p,
                            'is_dir': False, 'size': st.st_size, 'mtime': st.st_mtime,
                        })
                        added.append(os.path.basename(p))
                    keys.add(k)

            if keys:
                _SESSION['focus'] = keys
                if not _SESSION.get('sid'):
                    _SESSION['sid'] = uuid.uuid4().hex[:12]
            if first_dir and not _SESSION['root']:
                _SESSION['root'] = first_dir

        return {'ok': True, 'added': len(added), 'errors': errors[:8],
                'root': _SESSION['root'], 'sid': _SESSION.get('sid', '')}

    @staticmethod
    def add_by_name(payload):
        """
        拖放专用：浏览器不暴露绝对路径，只能拿文件名+大小。
        在已扫描的清单里匹配，命中就聚焦到这些条目。
        """
        if _session_stale(payload):
            return {'ok': False, 'error': '清单已被另一个页面更新，请重新扫描', 'stale': True}
        names = payload.get('names') or []
        if not isinstance(names, list):
            return {'ok': False, 'error': 'names 必须是数组'}

        def _key(p):
            return os.path.normcase(os.path.abspath(p))

        with _LOCK:
            items = list(_SESSION['items'])

        by_name = {}
        for i in items:
            by_name.setdefault(os.path.basename(i['name']).lower(), []).append(i)

        matched, missing, ambiguous, keys = [], [], [], set()
        for n in names:
            if not isinstance(n, dict):
                continue
            nm = (n.get('name') or '').strip()
            if not nm:
                continue
            try:
                sz = int(n.get('size', -1))
            except (TypeError, ValueError):
                sz = -1
            cands = by_name.get(nm.lower(), [])
            if len(cands) > 1 and sz >= 0:
                narrowed = [c for c in cands if int(c.get('size', 0)) == sz]
                if narrowed:
                    cands = narrowed
            if len(cands) == 1:
                keys.add(_key(cands[0]['path']))
                matched.append(nm)
            elif len(cands) > 1:
                ambiguous.append(nm)
            else:
                missing.append(nm)

        with _LOCK:
            if keys:
                _SESSION['focus'] = keys

        return {'ok': True, 'matched': len(matched), 'missing': missing[:8],
                'ambiguous': ambiguous[:8], 'total_items': len(items),
                'sid': _SESSION.get('sid', '')}

    @staticmethod
    def list_scope(payload=None):
        """显示当前清单范围：全部 / 仅聚焦。"""
        if _session_stale(payload or {}):
            return {'ok': False, 'error': '清单已被另一个页面更新，请重新扫描', 'stale': True}
        with _LOCK:
            _SESSION['focus'] = None
            return {'ok': True, 'total': len(_SESSION['items']), 'focus': 0}

    @staticmethod
    def clear_list(payload=None):
        if _session_stale(payload or {}):
            return {'ok': False, 'error': '清单已被另一个页面更新，请重新扫描', 'stale': True}
        with _LOCK:
            n = len(_SESSION['items'])
            _SESSION['items'] = []
            _SESSION['focus'] = None
            _SESSION['root'] = ''
            _SESSION['sid'] = ''
        return {'ok': True, 'cleared': n, 'sid': ''}

    # ---------- 资源管理器右键菜单 ----------
    @staticmethod
    def shell_status():
        return shell_ext.status()

    @staticmethod
    def shell_install(payload=None):
        return shell_ext.install()

    @staticmethod
    def shell_uninstall(payload=None):
        return shell_ext.uninstall()

    # ---------- 大模型 ----------
    @staticmethod
    def llm_config(payload):
        if payload.get('reset_prompt'):
            payload['prompt'] = llm.DEFAULT_PROMPT
        # 没填 key 就保留原值，避免前端显示掩码后误清空。
        # 显式传 null 才表示清空（save_conf 会区分）。
        if payload.get('api_key') == '':
            payload.pop('api_key')
        for key in ('base_url', 'model', 'prompt'):
            if key in payload and not isinstance(payload[key], str):
                return {'ok': False, 'error': '%s 必须是字符串' % key}
        cfg = llm.save_conf(payload)
        return {'ok': True, 'llm': {
            'ready': llm.is_ready(cfg), 'enabled': cfg['enabled'],
            'base_url': cfg['base_url'], 'model': cfg['model'],
            'has_key': bool(cfg.get('api_key')), 'prompt': cfg['prompt'],
        }}

    @staticmethod
    def llm_suggest(payload):
        stems = payload.get('stems') or []
        paths = payload.get('paths') or []
        if not isinstance(stems, list) or not all(isinstance(s, str) for s in stems):
            return {'ok': False, 'error': 'stems 必须是字符串数组'}
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            return {'ok': False, 'error': 'paths 必须是字符串数组'}
        if len(stems) > 200:
            return {'ok': False, 'error': '一次最多处理 200 条，请分批操作'}
        if not stems:
            return {'ok': False, 'error': '没有可处理的条目'}
        cfg = llm.load_conf()
        if not llm.is_ready(cfg):
            return {'ok': False, 'error': '模型尚未启用或未填写地址/模型名，请先在「AI 设置」里配置'}
        try:
            return llm.suggest(stems, payload.get('instruction') or '', cfg,
                               bool(payload.get('with_content')), paths)
        except Exception as e:
            return {'ok': False, 'error': '%s: %s' % (type(e).__name__, e), 'names': stems}

    @staticmethod
    def add_root(path):
        if not isinstance(path, str) or not path:
            return {'ok': False, 'error': '路径为空或类型错误'}
        real = os.path.abspath(path)
        if not os.path.isdir(real):
            return {'ok': False, 'error': '目录不存在'}
        # 必须走 normpath：不然 "D:\a" 和 "D:\a\" 会被当成两个目录重复入库
        np = os.path.normcase(os.path.normpath(real))
        # 不只比较根本身：C:\Windows\System32 这类系统目录子树也必须拒绝。
        if engine.is_path_blocked(real):
            return {'ok': False, 'error': '系统目录及其子目录不允许批量改名'}
        # 右键 launcher 是另一个进程，读改写必须整段持跨进程锁，
        # 否则两边各自基于旧 config 写回，会互相覆盖掉刚加的授权目录。
        with engine.config_lock():  # modified
            cfg = engine.load_config()  # modified
            if any(os.path.normcase(os.path.normpath(r)) == np  # modified
                   for r in cfg['trusted_roots']):  # modified
                return {'ok': True, 'trusted_roots': cfg['trusted_roots']}  # modified
            cfg['trusted_roots'].append(os.path.normpath(real))  # modified
            engine.save_config(cfg)  # modified
        return {'ok': True, 'trusted_roots': cfg['trusted_roots']}

    @staticmethod
    def remove_root(path):
        if not isinstance(path, str) or not path:
            return {'ok': False, 'error': '路径为空或类型错误'}
        with engine.config_lock():  # modified
            cfg = engine.load_config()  # modified
            np = os.path.normcase(os.path.normpath(os.path.abspath(path)))  # modified
            cfg['trusted_roots'] = [r for r in cfg['trusted_roots']  # modified
                                    if os.path.normcase(os.path.normpath(  # modified
                                        os.path.abspath(r))) != np]  # modified
            engine.save_config(cfg)  # modified
        return {'ok': True, 'trusted_roots': cfg['trusted_roots']}

    @staticmethod
    def pick_folder():
        """起一个独立进程弹系统目录选择框，避免 tkinter 与 HTTP 线程冲突。"""
        code = (
            'import tkinter as tk\n'
            'from tkinter import filedialog\n'
            'r = tk.Tk()\n'
            'r.withdraw()\n'
            'r.attributes("-topmost", True)\n'
            'p = filedialog.askdirectory(title="选择要批量改名的文件夹")\n'
            'print(p or "")\n'
        )
        try:
            out = subprocess.run([sys.executable, '-c', code],
                                 capture_output=True, text=True, timeout=600)
            path = (out.stdout or '').strip().splitlines()
            path = path[-1].strip() if path else ''
            if path:
                return {'ok': True, 'path': path}
            return {'ok': False, 'error': '未选择目录'}
        except Exception as e:
            return {'ok': False, 'error': '无法弹出目录选择框：%s' % e}

    # ---------- 扫描 ----------
    @staticmethod
    def scan(payload):
        if not isinstance(payload.get('root', ''), str):
            return {'ok': False, 'error': 'root 必须是字符串'}
        root = (payload.get('root') or '').strip().strip('"')
        recursive = bool(payload.get('recursive'))
        include_dirs = bool(payload.get('include_dirs'))
        if not isinstance(payload.get('keyword', ''), str):
            return {'ok': False, 'error': 'keyword 必须是字符串'}
        keyword = (payload.get('keyword') or '').strip()
        include = payload.get('include') or ''
        exclude = payload.get('exclude') or ''
        if not isinstance(include, str) or not isinstance(exclude, str):
            return {'ok': False, 'error': 'include/exclude 必须是字符串'}

        ok, why = engine.check_root_allowed(root)
        if not ok:
            return {'ok': False, 'error': why, 'need_auth': '尚未授权' in why}

        items, truncated = engine.scan(root, recursive, include_dirs, keyword,
                                       include, exclude)
        with _LOCK:
            _SESSION['root'] = os.path.abspath(root)
            _SESSION['items'] = items
            _SESSION['recursive'] = recursive
            _SESSION['focus'] = None       # 重新扫描即回到"全部"
            _SESSION['sid'] = uuid.uuid4().hex[:12]
        return {
            'ok': True,
            'root': os.path.abspath(root),
            'sid': _SESSION['sid'],
            'count': len(items),
            'truncated': truncated,
            'items': [{
                'name': i['name'], 'dir': i['dir'], 'is_dir': i['is_dir'],
                'size': i['size'], 'mtime': i['mtime'],
            } for i in items],
        }

    # ---------- 预览 ----------
    @staticmethod
    def preview(payload):
        if _session_stale(payload):
            return {'ok': False, 'error': '清单已被另一个页面更新，请重新扫描',
                    'stale': True}
        enabled = payload.get('enabled') if payload.get('enabled') is not None \
            else rules.default_enabled()
        params = payload.get('params') if payload.get('params') is not None \
            else rules.default_params()
        if not isinstance(enabled, dict) or not isinstance(params, dict):
            return {'ok': False, 'error': 'enabled/params 必须是对象'}
        sort_by = payload.get('sort_by') or 'name'
        target = payload.get('target') or 'name'
        if sort_by not in ('name', 'mtime', 'size', 'type', 'none'):
            return {'ok': False, 'error': '排序方式无效'}
        if target not in ('name', 'ext', 'both'):
            return {'ok': False, 'error': '改名目标无效'}

        with _LOCK:
            items = list(_SESSION['items'])
            root = _SESSION['root']
            focus = _SESSION.get('focus')

        # 拖放/粘贴进来的条目会形成"聚焦"，只预览这些；没有聚焦就预览全部
        if focus:
            items = [i for i in items
                     if os.path.normcase(os.path.abspath(i['path'])) in focus]
        if not items:
            return {'ok': False, 'error': '请先扫描目录，或把文件拖进来'}

        lower_ext = bool(payload.get('lower_ext'))
        auto_dedupe = bool(payload.get('auto_dedupe'))

        rows = engine.build_preview(
            items, enabled, params, lower_ext, auto_dedupe,
            sort_by=sort_by,
            sort_desc=bool(payload.get('sort_desc')),
            target=target,
        )
        stat = {
            'total': len(rows),
            'change': sum(1 for r in rows if r['status'] == 'ok'),
            'unchanged': sum(1 for r in rows if r['status'] == 'unchanged'),
            'conflict': sum(1 for r in rows if r['status'] == 'conflict'),
            'invalid': sum(1 for r in rows if r['status'] == 'invalid'),
        }
        with _LOCK:
            total_all = len(_SESSION['items'])
        stat['total_all'] = total_all
        stat['focused'] = len(rows) if focus else 0
        return {'ok': True, 'root': root, 'sid': _SESSION.get('sid', ''),
                'rows': rows, 'stat': stat, 'focused': bool(focus)}

    # ---------- 执行 ----------
    @staticmethod
    def execute(payload):
        if _session_stale(payload):
            return {'ok': False, 'error': '清单已被另一个页面更新，请重新扫描',
                    'stale': True}
        if not isinstance(payload.get('root', ''), str):
            return {'ok': False, 'error': 'root 必须是字符串'}
        root = (payload.get('root') or '').strip()
        picks = payload.get('items') or []
        if not isinstance(picks, list) or not all(isinstance(p, dict) for p in picks):
            return {'ok': False, 'error': 'items 必须是对象数组'}
        if not picks:
            return {'ok': False, 'error': '没有勾选任何条目'}
        try:
            res = engine.execute(root, picks, note=str(payload.get('note', '')))
            if res.get('done'):
                with _LOCK:
                    _SESSION['items'] = []
                    _SESSION['focus'] = None
                    _SESSION['root'] = ''
                    _SESSION['sid'] = ''
            return res
        except PermissionError as e:
            return {'ok': False, 'error': str(e)}
        except ValueError as e:
            return {'ok': False, 'error': str(e)}
        except Exception as e:
            return {'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}

    # ---------- 日志 ----------
    @staticmethod
    def journals():
        return {'ok': True, 'items': engine.list_journals()}

    @staticmethod
    def journal_detail(jid):
        j = engine.get_journal(jid)
        if not j:
            return {'ok': False, 'error': '日志不存在'}
        return {'ok': True, 'journal': j}

    @staticmethod
    def rollback(payload):
        jid = payload.get('id')
        if not jid:
            return {'ok': False, 'error': '缺少日志 id'}
        try:
            return engine.rollback(jid)
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    # ---------- 单条试验 ----------
    @staticmethod
    def test_name(payload):
        """给单个名字试跑规则，便于调参。"""
        name = payload.get('name') or ''
        if not isinstance(name, str):
            return {'ok': False, 'error': 'name 必须是字符串'}
        enabled = payload.get('enabled') if payload.get('enabled') is not None \
            else rules.default_enabled()
        params = payload.get('params') if payload.get('params') is not None \
            else rules.default_params()
        if not isinstance(enabled, dict) or not isinstance(params, dict):
            return {'ok': False, 'error': 'enabled/params 必须是对象'}
        if not name:
            return {'ok': False, 'error': '名字为空'}
        # 试名字只用来看规则效果，真实文件名上限 255；给足余量后仍要设上限，
        # 否则 4 MiB 请求体能塞进百万字符，跑完整条管线要十几秒。  # modified
        if len(name) > 1000:  # modified
            return {'ok': False, 'error': '名字过长（超过 1000 字符）'}  # modified
        st = rules.apply_pipeline(name, enabled, params, {'index': 0, 'total': 1, 'ext': ''})
        new_stem, applied, ctx = st
        return {
            'ok': True,
            'result': new_stem,
            'applied': applied,
            'notes': ctx.get('numbers', []),
            'rare': text_norm.find_rare_chars(new_stem),
        }


PRESETS = os.path.join(engine.DATA, 'presets.json')


def _preset_list(presets):
    """磁盘仍用 name->配置 字典，HTTP 返回数组；兼容旧版文件与前端。"""
    out = []
    for name, value in sorted(presets.items(), key=lambda kv: kv[0].lower()):
        if not isinstance(value, dict):
            continue           # 手工改坏的单条不应让整个方案列表 500
        out.append(dict({'name': name}, **value))
    return out


def _load_presets():
    try:
        if os.path.exists(PRESETS):
            with open(PRESETS, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            # 若曾由新接口写成数组，也能无损读回。
            if isinstance(data, list):
                return {p['name']: {k: v for k, v in p.items() if k != 'name'}
                        for p in data if isinstance(p, dict) and p.get('name')}
    except Exception:
        pass
    return {}


def _save_presets(d):
    os.makedirs(engine.DATA, exist_ok=True)
    tmp = PRESETS + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PRESETS)


def _common_dirs():
    home = os.path.expanduser('~')
    cands = [
        ('桌面', os.path.join(home, 'Desktop')),
        ('下载', os.path.join(home, 'Downloads')),
        ('文档', os.path.join(home, 'Documents')),
        ('图片', os.path.join(home, 'Pictures')),
    ]
    out = []
    for label, p in cands:
        if os.path.isdir(p):
            out.append({'label': label, 'path': p})
    for d in ('D:\\', 'E:\\', 'F:\\'):
        if os.path.isdir(d):
            out.append({'label': d.replace('\\', ''), 'path': d})
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = 'RenameTool/1.2'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def send_response(self, code, message=None):
        super().send_response(code, message)
        # 覆盖 _send、静态文件和标准库 501 错误页，统一加安全头。
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
        self.send_header('X-Content-Type-Options', 'nosniff')

    # 静默日志，保持控制台干净
    def log_message(self, fmt, *args):
        pass

    # ---------- 工具 ----------
    def _request_allowed(self):
        """只接受本机页面/本机进程请求，挡掉恶意网站对 localhost 的跨站调用。"""
        host = self.headers.get('Host', '')
        try:
            parsed_host = urllib.parse.urlparse('//' + host)
            hostname = parsed_host.hostname
        except ValueError:
            return False
        if (hostname or '').lower() not in ('127.0.0.1', 'localhost', '::1'):
            return False
        if parsed_host.port not in (None, self.server.server_port):
            return False
        origin = self.headers.get('Origin')
        if origin:
            try:
                o = urllib.parse.urlparse(origin)
                if (o.hostname or '').lower() not in ('127.0.0.1', 'localhost', '::1'):
                    return False
                if o.port not in (None, self.server.server_port):
                    return False
            except ValueError:
                return False
        return True

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        # chunked 未实现；若放进 keep-alive 会把残留 chunk 当下一请求解析。
        if self.headers.get('Transfer-Encoding'):
            self.close_connection = True
            raise ValueError('不支持 chunked 请求体')
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            self.close_connection = True
            raise ValueError('Content-Length 无效')
        if length < 0 or length > 4 * 1024 * 1024:
            # 先把已声明的小中型请求体读掉再返回 400，避免客户端收到 RST
            # 而看不到结构化错误；超大流量仍直接断开。
            if 0 < length <= 16 * 1024 * 1024:
                try:
                    self.rfile.read(length)
                except OSError:
                    pass
            self.close_connection = True
            raise ValueError('请求体过大')
        if not length:
            # 无 Content-Length 的 POST 不允许复用连接，避免客户端偷偷带 body 时错位。
            self.close_connection = True
            return {}
        ctype = (self.headers.get('Content-Type') or '').lower()
        if not ctype.startswith('application/json'):
            self.close_connection = True
            raise ValueError('Content-Type 必须是 application/json')
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode('utf-8'))
        except Exception:
            raise ValueError('JSON 格式无效')
        if not isinstance(data, dict):
            raise ValueError('JSON 顶层必须是对象')
        return data

    def _serve_file(self, rel):
        rel = rel.lstrip('/').replace('\\', '/')
        if '..' in rel.split('/'):
            self._send({'ok': False, 'error': 'bad path'}, 400)
            return
        path = os.path.realpath(os.path.join(WEB, os.path.normpath(rel)))
        # normpath + realpath 后确认归属，防止 /C:/...、UNC 或链接把路径带离 web/。
        try:
            inside = os.path.commonpath([os.path.realpath(WEB), path]) == os.path.realpath(WEB)
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(path):
            self._send({'ok': False, 'error': 'not found'}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', MIME.get(ext, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------
    def do_GET(self):
        if not self._request_allowed():
            self._send({'ok': False, 'error': '仅允许本机访问'}, 403)
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ('/', '/index.html'):
                self._serve_file('index.html')
            elif path == '/api/state':
                self._send(Api.state())
            elif path == '/api/journals':
                self._send(Api.journals())
            elif path == '/api/noise':
                self._send(Api.noise_get())
            elif path == '/api/shell':
                self._send(Api.shell_status())
            elif path == '/api/presets':
                self._send(Api.presets_get())
            elif path == '/api/journal':
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                self._send(Api.journal_detail((qs.get('id') or [''])[0]))
            elif path == '/api/ping':
                self._send({'ok': True, 'app': 'rename-tool-cn',
                            'version': VERSION, 'base': BASE})
            else:
                self._serve_file(path)
        except Exception:
            traceback.print_exc()
            self._send({'ok': False, 'error': '服务器内部错误'}, 500)

    def do_POST(self):
        if not self._request_allowed():
            self._send({'ok': False, 'error': '仅允许本机访问'}, 403)
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            payload = self._read_json()
            table = {
                '/api/roots/add': Api.add_root,
                '/api/roots/remove': Api.remove_root,
                '/api/pick-folder': lambda p: Api.pick_folder(),
                '/api/scan': Api.scan,
                '/api/preview': Api.preview,
                '/api/execute': Api.execute,
                '/api/rollback': Api.rollback,
                '/api/test-name': Api.test_name,
                '/api/llm-config': Api.llm_config,
                '/api/llm-suggest': Api.llm_suggest,
                '/api/noise': Api.noise_save,
                '/api/add-paths': Api.add_paths,
                '/api/add-by-name': Api.add_by_name,
                '/api/clear-list': Api.clear_list,
                '/api/show-all': Api.list_scope,
                '/api/shell/install': Api.shell_install,
                '/api/shell/uninstall': Api.shell_uninstall,
                '/api/presets': Api.presets_save,          # 旧入口保留
                '/api/presets/save': Api.presets_save,
                '/api/presets/delete': Api.presets_delete,
            }
            fn = table.get(path)
            if not fn:
                self._send({'ok': False, 'error': 'unknown api'}, 404)
                return
            if path in ('/api/roots/add', '/api/roots/remove'):
                res = fn(payload.get('path'))
            elif path in ('/api/pick-folder',):
                res = fn(payload)
            else:
                res = fn(payload)
            self._send(res)
        except ValueError as e:
            self._send({'ok': False, 'error': str(e)}, 400)
        except (TypeError, AttributeError) as e:
            self._send({'ok': False, 'error': '请求参数类型错误：%s' % e}, 400)
        except Exception:
            traceback.print_exc()
            self._send({'ok': False, 'error': '服务器内部错误，请查看控制台'}, 500)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        sock, addr = super().get_request()
        sock.settimeout(30)       # 防止半开连接永久占用线程
        return sock, addr


def _acquire_instance_lock():
    """同一工具目录只允许一个服务进程，避免两实例并发写同一份日志/配置。"""
    global _INSTANCE_LOCK
    f = None
    try:
        os.makedirs(engine.DATA, exist_ok=True)
        f = open(os.path.join(engine.DATA, 'app.lock'), 'a+b')
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            f.write(b'0')
            f.flush()
        f.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:                                       # pragma: no cover
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _INSTANCE_LOCK = f
        return True
    except OSError:
        if f is not None:
            f.close()
        return False


def _free_port(start=8770, end=8800):
    for p in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((HOST, p)) != 0:
                return p
    return 0


def _log_startup_error():
    try:
        os.makedirs(engine.DATA, exist_ok=True)
        with open(os.path.join(engine.DATA, 'app.log'), 'a', encoding='utf-8') as f:
            f.write('\n[%s]\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
            traceback.print_exc(file=f)
    except Exception:
        pass


def main():
    # 保证控制台能立刻看到提示（重定向/管道下 Python 默认是块缓冲）
    try:
        sys.stdout.reconfigure(line_buffering=True, encoding='utf-8')
        sys.stderr.reconfigure(line_buffering=True, encoding='utf-8')
    except Exception:
        pass
    os.makedirs(engine.JOURNALS, exist_ok=True)
    if not _acquire_instance_lock():
        print('已有同一目录的改名器在运行；请使用已打开的页面。')
        return 1
    port = _free_port()
    if not port:
        print('找不到可用端口')
        if _INSTANCE_LOCK:
            _INSTANCE_LOCK.close()
        return 1
    url = 'http://%s:%d/' % (HOST, port)
    srv = None
    for _ in range(8):
        try:
            srv = LocalServer((HOST, port), Handler)
            break
        except OSError:
            port = _free_port(port + 1)
            if not port:
                break
            url = 'http://%s:%d/' % (HOST, port)
    if srv is None:
        print('启动失败：找不到可绑定端口')
        if _INSTANCE_LOCK:
            _INSTANCE_LOCK.close()
        return 1
    print('=' * 56)
    print('  中文文件批量改名器  已启动')
    print('  地址：%s' % url)
    print('  关闭本窗口即退出')
    print('=' * 56)
    if not os.environ.get('RENAME_NO_BROWSER'):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已退出')
    finally:
        srv.server_close()
        if _INSTANCE_LOCK:
            _INSTANCE_LOCK.close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main() or 0)
    except Exception:
        _log_startup_error()
        raise
