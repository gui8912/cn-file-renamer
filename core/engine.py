# -*- coding: utf-8 -*-
"""
改名引擎：扫描、规划、执行、日志、回滚。

安全设计（这几条是刻意的，不要简化）：
1. 目录白名单：未登记过的目录一律拒绝，杜绝误扫 C 盘
2. 系统目录硬黑名单：即使被加入白名单也拒绝
3. 两阶段改名：先全部改成临时名，再改成目标名
   这样 a->b、b->c 这种链式/交换改名不会互相覆盖
4. 执行前二次校验：前端传回的新名要在后端重新过一遍合法性
5. 执行前落盘计划：日志先写盘再动手，crash 也能回滚
"""

import contextlib  # modified: config_lock 用的上下文管理器
import fnmatch
import json
import os
import re
import stat
import threading
import time
import uuid

from . import rules, text_norm, validate

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, 'data')
JOURNALS = os.path.join(DATA, 'journals')
CONFIG = os.path.join(DATA, 'config.json')

# HTTP 服务是多线程的；进程内所有写盘改名必须串行，避免同一日志被并发改写。
_OP_LOCK = threading.RLock()


# ============================================================
# 配置 / 白名单
# ============================================================

_DEFAULT_CONFIG = {
    'trusted_roots': [],
    'max_scan': 5000,
    'journal_keep': 300,
    'lower_ext': False,
}


def load_config():
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            for k, v in _DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return dict(_DEFAULT_CONFIG)


@contextlib.contextmanager  # modified
def config_lock():  # modified
    """跨进程配置锁。

    launcher.py 是独立进程，网页端 add_root 和右键自动授权可能同时做
    "读 config -> 加一个目录 -> 写回"。只保证写文件原子还不够，后写的
    会把先写的授权整个覆盖掉（lost update）。调用方应把整段读改写包进来：

        with engine.config_lock():
            cfg = engine.load_config()
            ...
            engine.save_config(cfg)
    """
    os.makedirs(DATA, exist_ok=True)  # modified
    path = os.path.join(DATA, 'config.lock')  # modified
    with _OP_LOCK:  # modified: 进程内也串行，避免同进程多 fd 上锁语义差异
        with open(path, 'a+b') as f:  # modified
            f.seek(0, os.SEEK_END)  # modified
            if f.tell() == 0:  # modified
                f.write(b'0')  # modified: 锁第 0 字节，文件不能是空的
                f.flush()  # modified
            f.seek(0)  # modified
            if os.name == 'nt':  # modified
                import msvcrt  # modified
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # modified
            else:  # pragma: no cover  # modified
                import fcntl  # modified
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)  # modified
            try:  # modified
                yield  # modified
            finally:  # modified
                f.seek(0)  # modified
                if os.name == 'nt':  # modified
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)  # modified
                else:  # pragma: no cover  # modified
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)  # modified


def save_config(cfg):
    os.makedirs(DATA, exist_ok=True)
    # 日志写入早已改成唯一临时名，这里仍是固定 CONFIG + '.tmp'。
    # launcher.py 是独立进程，右键自动授权时可能与服务端同时写白名单：
    # 两边互踩同一个临时文件会让刚加的授权丢失，os.replace 还会抛异常。
    # 对齐 _write_journal：唯一临时名 + fsync + 原子替换 + 退避重试 + 失败清理。
    tmp = '%s.tmp.%s' % (CONFIG, uuid.uuid4().hex)  # modified
    try:  # modified
        for attempt in range(6):  # modified
            try:  # modified
                with open(tmp, 'w', encoding='utf-8') as f:  # modified
                    json.dump(cfg, f, ensure_ascii=False, indent=2)  # modified
                    f.flush()  # modified
                    os.fsync(f.fileno())  # modified
                os.replace(tmp, CONFIG)  # modified
                return  # modified
            except OSError:  # modified
                # Windows 上并发 replace 到同一目标、杀软/同步盘短暂占用都会报
                # PermissionError(13)，与 _write_journal 一样退避重试。
                if attempt == 5:  # modified
                    raise  # modified
                time.sleep(0.03 * (attempt + 1))  # modified
    finally:  # modified
        if os.path.exists(tmp):  # modified
            try:  # modified
                os.unlink(tmp)  # modified
            except OSError:  # modified
                pass  # modified


def _is_unc(p):
    r"""网络共享和 \\?\ 扩展路径不做批量改名，避免绕过盘符黑名单。"""
    return isinstance(p, str) and (p.startswith('\\\\') or p.startswith('//'))


def _normpath(p):
    # realpath 解析符号链接/ junction，防止授权目录里的链接把操作引到目录外
    return os.path.normcase(os.path.normpath(os.path.realpath(os.path.abspath(p))))


def _blocked_exact_roots():
    """只封锁根本身；用户桌面、文档等 USERPROFILE 子目录仍可使用。"""
    out = []
    home = os.environ.get('USERPROFILE')
    if home:
        out.append(home)
    out += [letter + ':\\' for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ']
    return {_normpath(p) for p in out}


def _blocked_tree_roots():
    """系统目录及其全部子目录都封锁。"""
    out = []
    env = os.environ
    for k in ('SystemRoot', 'windir', 'ProgramFiles', 'ProgramFiles(x86)',
              'ProgramData', 'APPDATA', 'LOCALAPPDATA'):
        v = env.get(k)
        if v:
            out.append(v)
    out += [r'C:\Windows', r'C:\Program Files', r'C:\Program Files (x86)', r'C:\ProgramData',
            r'C:\$Recycle.Bin', r'C:\Recovery', r'C:\PerfLogs',
            r'C:\System Volume Information', r'C:\Windows.old']
    return {_normpath(p) for p in out}


def blocked_roots():
    """兼容原有调用：返回精确封锁与整棵封锁的并集。"""
    return _blocked_exact_roots() | _blocked_tree_roots()


def _is_blocked_norm(np, exact_roots, tree_roots):
    if np in exact_roots:
        return True
    for root in tree_roots:
        prefix = root.rstrip(os.sep) + os.sep
        if np == root or np.startswith(prefix):
            return True
    return False


def is_path_blocked(path):
    if not path or _is_unc(path):
        return True
    np = _normpath(path)
    return _is_blocked_norm(np, _blocked_exact_roots(), _blocked_tree_roots())


def check_root_allowed(path):
    """返回 (ok, reason)"""
    if not path:
        return False, '路径为空'
    if _is_unc(path):
        return False, '不支持网络共享路径，请映射为本地盘符后使用'
    real = os.path.abspath(path)
    if not os.path.isdir(real):
        return False, '目录不存在或不是目录'
    np = _normpath(real)

    if _is_blocked_norm(np, _blocked_exact_roots(), _blocked_tree_roots()):
        return False, '系统目录受保护，不允许批量改名'

    cfg = load_config()
    for tr in cfg['trusted_roots']:
        trn = _normpath(tr)
        if np == trn:
            return True, ''
        if np.startswith(trn.rstrip(os.sep) + os.sep):
            return True, ''          # 子目录随父目录授权
    return False, '该目录尚未授权，请先在「目录授权」里添加'


# ============================================================
# 扫描
# ============================================================

def _is_reparse_dir(path):
    """符号链接、junction、挂载点都不遍历，避免越出授权目录或形成环。"""
    try:
        if os.path.islink(path):
            return True
        attrs = getattr(os.lstat(path), 'st_file_attributes', 0)
        return bool(attrs & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))
    except OSError:
        return True


def _split_patterns(value):
    if not value:
        return []
    parts = re.split(r'[;；|]+', str(value))
    out = []
    for p in parts:
        p = p.strip().lower()
        if not p:
            continue
        if not any(ch in p for ch in '*?'):
            # 不带通配符时按“包含此文本”处理：pdf 能命中 .pdf，
            # 2026 也能命中“会议2026年…”，不会被误补成 *.2026。
            p = '*' + p + '*'
        out.append(p)
    return out


def name_matches_filters(name, include='', exclude=''):
    """扫描范围筛选；include 为空表示全部，exclude 优先。"""
    low = name.lower()
    inc = _split_patterns(include)
    exc = _split_patterns(exclude)
    if inc and not any(fnmatch.fnmatchcase(low, p) for p in inc):
        return False
    if any(fnmatch.fnmatchcase(low, p) for p in exc):
        return False
    return True


def scan(root, recursive=False, include_dirs=False, keyword='', include='', exclude=''):
    cfg = load_config()
    limit = int(cfg.get('max_scan', 5000))
    out = []
    truncated = False

    if not os.path.isdir(root):
        raise ValueError('目录不存在')

    def _emit(entry_dir, name, is_dir, size, mtime):
        nonlocal truncated
        # 先过滤再计数，否则前 limit 个都不匹配时会把名额耗光
        if keyword and keyword.lower() not in name.lower():
            return True
        if is_dir and not include_dirs:
            return True
        # 筛选针对文件类型；勾“包含文件夹”时目录仍进入清单，便于单独整理目录名。
        if not is_dir and not name_matches_filters(name, include, exclude):
            return True
        if len(out) >= limit:
            truncated = True
            return False
        out.append({
            'name': name,
            'dir': entry_dir,
            'path': os.path.join(entry_dir, name),
            'is_dir': is_dir,
            'size': size,
            'mtime': mtime,
        })
        return True

    if recursive:
        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs
                       if d not in ('$RECYCLE.BIN', 'System Volume Information')
                       and not _is_reparse_dir(os.path.join(cur, d))]
            for d in sorted(dirs):
                p = os.path.join(cur, d)
                try:
                    st = os.stat(p)
                    if not _emit(cur, d, True, 0, st.st_mtime):
                        return out, truncated
                except OSError:
                    pass
            for fn in sorted(files):
                p = os.path.join(cur, fn)
                try:
                    st = os.stat(p)
                    if not _emit(cur, fn, False, st.st_size, st.st_mtime):
                        return out, truncated
                except OSError:
                    pass
    else:
        with os.scandir(root) as it:
            entries = sorted(it, key=lambda e: e.name.lower())
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if not include_dirs:
                        continue
                    if _is_reparse_dir(e.path):
                        continue
                    st = e.stat(follow_symlinks=False)
                    if not _emit(root, e.name, True, 0, st.st_mtime):
                        break
                elif e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    if not _emit(root, e.name, False, st.st_size, st.st_mtime):
                        break
            except OSError:
                continue
    return out, truncated


# ============================================================
# 预览
# ============================================================

def _time_ctx(ts):
    try:
        t = time.localtime(ts or time.time())
        return {
            'mtime_str': time.strftime('%Y-%m-%d', t),
            'mtime_full': time.strftime('%Y-%m-%d %H-%M-%S', t),
            'year': t.tm_year, 'month': '%02d' % t.tm_mon, 'day': '%02d' % t.tm_mday,
        }
    except Exception:
        return {'mtime_str': '', 'mtime_full': '', 'year': '', 'month': '', 'day': ''}


def sort_items(items, by='name', desc=False):
    """
    排序会决定序号的顺序，所以必须在生成序号之前做。
    默认 name 与扫描时已排好的顺序一致。
    """
    if by in ('none', '', None):
        return list(items)
    key = {
        # 先按目录再按名称，保证同一目录的文件聚在一起（否则跨目录同名文件会混排）
        'name': lambda i: (os.path.normcase(i['dir']), os.path.basename(i['name']).lower()),
        'mtime': lambda i: (os.path.normcase(i['dir']), i.get('mtime', 0)),
        'size': lambda i: (os.path.normcase(i['dir']), i.get('size', 0)),
        'type': lambda i: (os.path.normcase(i['dir']),
                           validate.split_ext(i['name'])[1].lower(),
                           os.path.basename(i['name']).lower()),
    }.get(by)
    if not key:
        return list(items)
    return sorted(items, key=key, reverse=bool(desc))


def build_preview(items, enabled=None, params=None, lower_ext=False, auto_dedupe=False,
                  sort_by='name', sort_desc=False, target='name'):
    """
    输入扫描结果，输出带新名的列表。

    target: 'name' 只改主名（默认）；'ext' 只改扩展名；'both' 整个文件名一起改
    """
    # 必须用 is None 判断：{} 是"全关"，被 or 替换成默认值会让用户关不掉规则
    if enabled is None:
        enabled = rules.default_enabled()
    if params is None:
        params = rules.default_params()

    items = sort_items(items, sort_by, sort_desc)
    total = len(items)
    out = []
    dir_seen = {}

    for i, it in enumerate(items):
        name = it['name']
        is_dir = it.get('is_dir', False)
        stem, ext = (name, '') if is_dir else validate.split_ext(name)

        dkey = os.path.normcase(it.get('dir', ''))
        dir_idx = dir_seen.get(dkey, 0)
        dir_seen[dkey] = dir_idx + 1

        size = int(it.get('size', 0) or 0)
        ctx = {
            'index': i, 'dir_index': dir_idx, 'total': total, 'ext': ext,
            'old_stem': stem,
            'parent': os.path.basename(os.path.normpath(it.get('dir', ''))),
            'grandparent': os.path.basename(
                os.path.dirname(os.path.normpath(it.get('dir', '') + os.sep))) or '',
            'size': size,
            'size_kb': round(size / 1024.0, 1) if size else 0,
            'size_mb': round(size / 1048576.0, 2) if size else 0,
        }
        ctx.update(_time_ctx(it.get('mtime')))
        try:
            cts = os.path.getctime(it['path'])
            c = _time_ctx(cts)
            ctx['ctime_str'] = c['mtime_str']
        except Exception:
            ctx['ctime_str'] = ''

        if target == 'ext' and not is_dir:
            if ext:
                body = ext.lstrip('.')
                nb, applied, ctx = rules.apply_pipeline(body, enabled, params, ctx)
                nb = stem + (('.' + nb) if nb else '')
            else:
                nb, applied = name, []     # 没有扩展名可改，原样保留（不能退回去改主名）
        elif target == 'both' or is_dir:
            nb, applied, ctx = rules.apply_pipeline(name, enabled, params, ctx)
        else:
            nb, applied, ctx = rules.apply_pipeline(stem, enabled, params, ctx)
            nb = (nb or stem) + ext
        new_name = nb or name

        # 扩展名小写：只在扩展名没被规则换掉时才做。
        # 否则模板把 .txt 改成 .md 后，这里会拿旧扩展名把它换回去
        if lower_ext and ext:
            base, cur_ext = validate.split_ext(new_name)
            if cur_ext and cur_ext.lower() == ext.lower():
                new_name = base + cur_ext.lower()

        if not is_dir:
            new_name = text_norm.strip_edges(new_name)
        else:
            new_name = new_name.strip(' .')

        status, reason = 'ok', ''
        if new_name == name:
            status = 'unchanged'
            reason = '规则未产生变化'
        else:
            ok, why = validate.check(new_name)
            if ok and os.name == 'nt':
                full_dst = os.path.join(it['dir'], new_name)
                if validate.utf16_len(os.path.abspath(full_dst)) >= 260:
                    ok, why = False, '完整路径超过 260 字，请缩短目录层级'
            if not ok:
                status, reason = 'invalid', why

        # 前端提示用的附加信息
        notes = []
        if ctx.get('date'):
            notes.append('识别到日期 %s（原文 %s）' % (ctx['date'], ctx.get('date_raw', '')))
        for c in ctx.get('numbers', []):
            notes.append('数字 %s → %s' % (c['from'], c['to']))
        if ctx.get('noise'):
            notes.append('移除噪声词')
        if ctx.get('regex_hits'):
            notes.append('正则命中 %d 处' % ctx['regex_hits'])
        rare = text_norm.find_rare_chars(validate.split_ext(new_name)[0])
        if rare:
            notes.append('含非常用字：%s' % ''.join(rare))
        if ctx.get('errors'):
            notes.append('规则异常：%s' % '; '.join(ctx['errors']))

        out.append({
            'id': uuid.uuid4().hex[:12],
            'path': it['path'],
            'dir': it['dir'],
            'is_dir': is_dir,
            'old_name': name,
            'new_name': new_name,
            'ext': ext,
            'size': it.get('size', 0),
            'mtime': it.get('mtime', 0),
            'status': status,
            'reason': reason,
            'applied': applied,
            'notes': notes,
            'selected': status == 'ok',
        })

    # 同批内部冲突检测
    seen = {}
    for r in out:
        if r['status'] in ('invalid',):
            continue
        key = os.path.normcase(r['new_name'])
        seen.setdefault((os.path.normcase(r['dir']), key), []).append(r)
    for (_, key), group in seen.items():
        if len(group) > 1:
            for r in group:
                if r['status'] == 'ok':
                    r['status'] = 'conflict'
                    r['reason'] = '同批内有 %d 个文件重名' % len(group)
                    r['selected'] = False

    # 与磁盘上已有文件冲突（注意别用 target 这个变量名，它是本函数的参数）
    # 先建集合再判断，避免在循环里对每一项都全表扫描（O(n²)）
    batch_paths = {os.path.normcase(os.path.abspath(r['path'])) for r in out}
    for r in out:
        if r['status'] != 'ok':
            continue
        if r['new_name'] == r['old_name']:
            continue
        dst_path = os.path.join(r['dir'], r['new_name'])
        if os.path.exists(dst_path) and os.path.normcase(dst_path) != os.path.normcase(r['path']):
            # 若目标本身也在本批改名列表里，两阶段改名可解决，不算冲突
            if not _in_batch_target(batch_paths, dst_path):
                r['status'] = 'conflict'
                r['reason'] = '目标名已存在'
                r['selected'] = False

    if auto_dedupe:
        out = auto_resolve_conflicts(out)

    # 父目录改名后，子项的旧绝对路径立即失效。两阶段改名只适用于平级路径，
    # 因此同一批里一旦出现“目录 + 其内部条目”，必须先拆成两批。
    path_keys = [(r, _normpath(r['path'])) for r in out]
    for r, key in path_keys:
        if not r['is_dir'] or r['status'] != 'ok':
            continue
        prefix = key.rstrip(os.sep) + os.sep
        if any(other is not r and p.startswith(prefix) for other, p in path_keys):
            r['status'] = 'conflict'
            r['reason'] = '该目录内部还有同批条目，请先改内部或分两批处理'
            r['selected'] = False

    return out


def _in_batch_target(batch_paths, target):
    """batch_paths 是 normcase(abspath) 的集合，O(1) 判定。"""
    return os.path.normcase(os.path.abspath(target)) in batch_paths


def auto_resolve_conflicts(items):
    """给冲突项自动加 (1)(2)…，直到不冲突。"""
    occupied = set()
    for r in items:
        if r['status'] in ('ok', 'unchanged'):
            # 原名在本批完成前仍占用；新名在本批完成后会占用，两者都要保护。
            occupied.add((os.path.normcase(r['dir']), os.path.normcase(r['old_name'])))
            occupied.add((os.path.normcase(r['dir']), os.path.normcase(r['new_name'])))

    for r in items:
        if r['status'] != 'conflict':
            continue
        stem, ext = validate.split_ext(r['new_name'])
        n = 1
        while n < 999:
            cand = '%s(%d)%s' % (stem, n, ext)
            key = (os.path.normcase(r['dir']), os.path.normcase(cand))
            if key not in occupied and not os.path.exists(os.path.join(r['dir'], cand)):
                r['new_name'] = cand
                r['status'] = 'ok'
                r['reason'] = '冲突已自动加序号'
                r['selected'] = True
                occupied.add(key)
                break
            n += 1
    return items


# ============================================================
# 执行
# ============================================================

def _journal_path(jid):
    return os.path.join(JOURNALS, '%s.json' % jid)


def _write_journal(j):
    os.makedirs(JOURNALS, exist_ok=True)
    p = _journal_path(j['id'])
    # 并发写同一日志时固定 .tmp 会互相覆盖/抛 PermissionError。
    tmp = '%s.tmp.%s' % (p, uuid.uuid4().hex)
    last = None
    try:
        for attempt in range(6):
            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(j, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())   # 改名日志必须先真正落盘，再碰文件
                os.replace(tmp, p)
                return
            except OSError as e:
                last = e
                # Windows 杀软/同步盘/并发读可能短暂占用日志；退避重试。
                if attempt == 5:
                    raise
                time.sleep(0.03 * (attempt + 1))
        if last:
            raise last
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _temp_path(path, prefix):
    """生成短临时名；不能拼上原名，否则 255 字长名会在第一阶段就失败。"""
    return os.path.join(os.path.dirname(path), '.~%s~%s' % (prefix, uuid.uuid4().hex))


def _fingerprint(path):
    """
    记录文件身份，不能只靠路径判断“这是不是当初那个文件”。
    NTFS 上 inode（file index）跨 rename 保持不变；网络盘不提供 inode 时退回
    大小 + 修改时间 + 类型。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return {
        'dev': int(getattr(st, 'st_dev', 0) or 0),
        'ino': int(getattr(st, 'st_ino', 0) or 0),
        'size': int(getattr(st, 'st_size', 0) or 0),
        'mtime_ns': int(getattr(st, 'st_mtime_ns', 0) or 0),
        'is_dir': stat.S_ISDIR(st.st_mode),
    }


def _same_identity(fp, path):
    if not fp:
        return True                    # 兼容没有指纹的旧日志
    st_fp = _fingerprint(path)
    if not st_fp:
        return False
    if fp.get('is_dir') != st_fp.get('is_dir'):
        return False
    if fp.get('dev') and fp.get('ino') and st_fp.get('dev') and st_fp.get('ino'):
        return fp['dev'] == st_fp['dev'] and fp['ino'] == st_fp['ino']
    return (fp.get('size') == st_fp.get('size')
            and fp.get('mtime_ns') == st_fp.get('mtime_ns'))


def _parent_in_set(path, path_set):
    """path 的任一祖先是 path_set 成员时返回该祖先（按 realpath 处理 junction 别名）。"""
    cur = os.path.dirname(_normpath(path))
    while True:
        key = os.path.normcase(cur)
        if key in path_set:
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return ''
        cur = parent


def execute(root, pairs, note=''):
    """串行化执行入口；同一进程内不允许两个批次同时碰磁盘。"""
    with _OP_LOCK:
        return _execute_impl(root, pairs, note)


def _execute_impl(root, pairs, note=''):
    """
    pairs: [{'src': 绝对路径, 'new_name': 新文件名, 'old_name': 原文件名}]
    返回 {ok, journal_id, done, failed:[...], skipped:[...]}
    """
    # root 只做整批预检；拖放/粘贴场景下 root 可能为空，那就靠下面逐个文件校验
    if root:
        ok_root, why = check_root_allowed(root)
        if not ok_root:
            raise PermissionError(why)

    # 白名单只读一次。原来每个文件都调一次 load_config()，几百个文件就是几百次读盘
    cfg_cache = load_config()
    blocked_exact_cache = _blocked_exact_roots()
    blocked_tree_cache = _blocked_tree_roots()

    def _dir_allowed(d):
        if _is_unc(d):
            return False, '不支持网络共享路径'
        dn = _normpath(d)
        if _is_blocked_norm(dn, blocked_exact_cache, blocked_tree_cache):
            return False, '系统目录受保护，不允许批量改名'
        for tr in cfg_cache['trusted_roots']:
            trn = _normpath(tr)
            if dn == trn or dn.startswith(trn + os.sep):
                return True, ''
        return False, '该目录尚未授权'

    # ---- 1. 后端二次校验 ----
    valid = []
    skipped = []
    seen_sources = set()
    for p in pairs:
        # 畸形条目（缺 src / 类型不对）不能让整批抛 KeyError，
        # 原来会一路冒到 Api.execute 变成 "KeyError: 'src'" 这种内部错误信息。
        if not isinstance(p, dict):  # modified
            skipped.append({'src': '（无效条目）', 'reason': '条目不是对象'})  # modified
            continue  # modified
        raw_src = p.get('src')  # modified
        if not isinstance(raw_src, str) or not raw_src.strip():  # modified
            skipped.append({'src': '（无效条目）', 'reason': '缺少合法的源文件路径'})  # modified
            continue  # modified
        src = os.path.abspath(raw_src)  # modified
        src_key = os.path.normcase(src)
        new_name = (p.get('new_name') or '').strip()
        if src_key in seen_sources:
            skipped.append({'src': src, 'reason': '同一源文件被重复提交'})
            continue
        seen_sources.add(src_key)
        if not os.path.exists(src):
            skipped.append({'src': src, 'reason': '源文件不存在'})
            continue
        if not new_name:
            skipped.append({'src': src, 'reason': '新名为空'})
            continue
        ok, reason = validate.check(new_name)
        if not ok:
            skipped.append({'src': src, 'reason': reason})
            continue
        if os.path.basename(src) == new_name:
            skipped.append({'src': src, 'reason': '新旧同名，无需改名'})
            continue
        # 源文件所在的目录必须在授权范围内
        ok2, why2 = _dir_allowed(os.path.dirname(src))
        if not ok2:
            skipped.append({'src': src, 'reason': why2})
            continue
        dst = os.path.join(os.path.dirname(src), new_name)
        if os.name == 'nt' and validate.utf16_len(os.path.abspath(dst)) >= 260:
            skipped.append({'src': src, 'reason': '完整路径超过 260 字，请缩短目录层级'})
            continue
        fp = _fingerprint(src)
        if not fp:
            skipped.append({'src': src, 'reason': '读取文件身份失败'})
            continue
        valid.append({
            'src': src,
            'dst': dst,
            'old_name': os.path.basename(src),
            'new_name': new_name,
            'fp': fp,
        })

    if not valid:
        return {'ok': False, 'journal_id': None, 'done': 0, 'failed': [], 'skipped': skipped}

    # 父子路径同批时，父目录改名会让子项旧路径失效；必须在碰盘前整批拒绝。
    src_keys = {_normpath(v['src']) for v in valid}
    for v in valid:
        ancestor = _parent_in_set(v['src'], src_keys)
        if ancestor:
            raise ValueError('同一批不能同时改名目录及其内部条目：%s；请分两批处理'
                             % os.path.basename(ancestor))

    # ---- 2. 目标冲突检测 ----
    targets = {}
    for v in valid:
        k = (os.path.normcase(os.path.dirname(v['dst'])), os.path.normcase(v['new_name']))
        targets.setdefault(k, []).append(v)
    dup = {k for k, g in targets.items() if len(g) > 1}
    if dup:
        bad = []
        for k in dup:
            for v in targets[k]:
                bad.append(v['new_name'])
        raise ValueError('存在重复目标名：%s' % '、'.join(sorted(set(bad))[:5]))

    # 磁盘上已存在的目标（且不在本次源集合里）
    src_set = {os.path.normcase(os.path.abspath(v['src'])) for v in valid}
    disk_blocked = set()
    for v in valid:
        if os.path.exists(v['dst']) and os.path.normcase(os.path.abspath(v['dst'])) not in src_set:
            skipped.append({'src': v['src'], 'reason': '目标已存在：%s' % v['new_name']})
            disk_blocked.add(os.path.normcase(v['src']))
    if disk_blocked:
        # 只能过滤本阶段确认的冲突项；skipped 里还可能有“重复源”等输入问题，
        # 若按整个 skipped 过滤，会把同一源的第一条合法任务也误删掉。
        valid = [v for v in valid if os.path.normcase(v['src']) not in disk_blocked]

    # 级联跳过：a->b 依赖 b 先腾空；若 b->c 已因冲突被跳过，a->b 也必须跳过，
    # 否则第二阶段会撞上仍在原位的 b。
    changed = True
    while changed and valid:
        changed = False
        blocked_keys = {os.path.normcase(os.path.abspath(s['src'])) for s in skipped
                        if s.get('reason', '').startswith('目标已存在')}
        remain = []
        for v in valid:
            if os.path.normcase(os.path.abspath(v['dst'])) in blocked_keys:
                skipped.append({'src': v['src'],
                                'reason': '依赖的目标文件未腾空：%s' % v['new_name']})
                disk_blocked.add(os.path.normcase(v['src']))
                changed = True
            else:
                remain.append(v)
        valid = remain

    if not valid:
        return {'ok': False, 'journal_id': None, 'done': 0, 'failed': [], 'skipped': skipped}

    # ---- 3. 先落盘计划，再动手 ----
    jid = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    journal = {
        'id': jid,
        'root': os.path.abspath(root) if root else '',
        'created': time.time(),
        'created_str': time.strftime('%Y-%m-%d %H:%M:%S'),
        'note': note,
        'status': 'running',
        'moves': [],
        'skipped': skipped,
    }
    _write_journal(journal)

    # ---- 4. 两阶段改名 ----
    # 在任何一次 os.rename 之前，就把全部临时路径写进日志。即使第一阶段中途断电，
    # 下次也能根据 src/tmp/dst 三处的实际状态恢复，不会留下无人知晓的临时文件。
    staged = [{'orig': v, 'tmp': _temp_path(v['src'], 'ren')} for v in valid]
    journal['status'] = 'staging'
    journal['staged'] = [{'src': s['orig']['src'], 'tmp': s['tmp'],
                          'dst': s['orig']['dst'], 'new_name': s['orig']['new_name'],
                          'old_name': s['orig']['old_name'], 'fp': s['orig']['fp']}
                         for s in staged]
    _write_journal(journal)

    failed = []
    active = []
    for s in staged:
        v = s['orig']
        if not _same_identity(v['fp'], v['src']):
            failed.append({'src': v['src'], 'reason': '源文件身份已变化，已跳过'})
            continue
        try:
            os.rename(v['src'], s['tmp'])
            active.append(s)
        except OSError as e:
            failed.append({'src': v['src'], 'reason': _err(e)})

    done = 0
    unresolved = []
    for s in active:
        v = s['orig']
        try:
            os.rename(s['tmp'], v['dst'])
            done += 1
            journal['moves'].append({
                'src': v['src'], 'dst': v['dst'],
                'old_name': v['old_name'], 'new_name': v['new_name'],
                'ok': True, 'fp': v['fp'],
            })
        except OSError as e:
            # 改名失败：把临时名恢复回原名，避免文件失踪
            try:
                os.rename(s['tmp'], v['src'])
                restored = True
            except OSError:
                restored = False
            failed.append({
                'src': v['src'],
                'reason': _err(e) + ('' if restored else '（文件停在临时名：%s）' % s['tmp']),
            })
            journal['moves'].append({
                'src': v['src'], 'dst': v['dst'],
                'old_name': v['old_name'], 'new_name': v['new_name'],
                'ok': False, 'error': _err(e), 'restored': restored,
                'tmp': s['tmp'], 'fp': v['fp'],
            })
            if not restored:
                unresolved.append({
                    'src': v['src'], 'tmp': s['tmp'], 'dst': v['dst'],
                    'old_name': v['old_name'], 'new_name': v['new_name'],
                    'fp': v['fp'], 'orphan': True,
                })

    if unresolved:
        journal['status'] = 'partial_orphan'
    elif failed:
        journal['status'] = 'partial' if done else 'failed'
    else:
        journal['status'] = 'done'
    journal['finished'] = time.time()
    journal['done'] = done
    journal['failed'] = failed
    # 只保留真正停在临时名、还没恢复的条目；不能无条件清空，否则无法找回归属。
    journal['staged'] = unresolved
    journal_warning = ''
    try:
        _write_journal(journal)
    except OSError as e:
        # 文件操作已经结束；日志落盘失败只能警告，不能把已完成改名谎报成失败。
        journal_warning = '日志收尾写入失败：%s；可稍后用历史记录恢复' % e
    _prune_journals()

    result = {
        'ok': not failed and bool(done),
        'journal_id': jid,
        'done': done,
        'failed': failed,
        'skipped': skipped,
    }
    if journal_warning:
        result['warning'] = journal_warning
    return result


def _err(e):
    if isinstance(e, PermissionError):
        return '权限不足或被占用'
    return '%s: %s' % (type(e).__name__, e)


def _prune_journals():
    """只修剪已经完整回滚的旧日志；未恢复的记录必须保留。"""
    cfg = load_config()
    keep = int(cfg.get('journal_keep', 300))
    removable = []
    try:
        for fn in os.listdir(JOURNALS):
            if not fn.endswith('.json'):
                continue
            p = os.path.join(JOURNALS, fn)
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    j = json.load(f)
                if j.get('rolled_back'):
                    removable.append(p)
            except Exception:
                continue
        removable.sort(key=os.path.getmtime, reverse=True)
        for p in removable[keep:]:
            os.remove(p)
    except OSError:
        pass


# ============================================================
# 日志 / 回滚
# ============================================================

def list_journals(limit=50):
    """读接口也串行化；避免 execute 正在 replace 日志时读到半截/共享冲突。"""
    with _OP_LOCK:
        return _list_journals_impl(limit)


def _list_journals_impl(limit=50):
    if not os.path.isdir(JOURNALS):
        return []
    out = []
    for fn in os.listdir(JOURNALS):
        if not fn.endswith('.json'):
            continue
        p = os.path.join(JOURNALS, fn)
        try:
            with open(p, 'r', encoding='utf-8') as f:
                j = json.load(f)
            out.append({
                'id': j.get('id'),
                'root': j.get('root'),
                'created_str': j.get('created_str'),
                'created': j.get('created', 0),
                'status': j.get('status'),
                'done': j.get('done', 0),
                'total': len(j.get('moves', [])),
                'note': j.get('note', ''),
                'rolled_back': j.get('rolled_back', False),
            })
        except Exception:
            continue
    out.sort(key=lambda x: x['created'], reverse=True)
    return out[:limit]


def get_journal(jid):
    with _OP_LOCK:
        return _get_journal_impl(jid)


def _get_journal_impl(jid):
    # jid 来自 HTTP 参数，绝不能让它参与目录穿越；合法 ID 只由日期、UUID 和连字符组成。
    if not isinstance(jid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', jid):
        return None
    p = _journal_path(jid)
    if not os.path.exists(p):
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:
            j = json.load(f)
        if not isinstance(j, dict):
            return None
        j['moves'] = j.get('moves') if isinstance(j.get('moves'), list) else []
        j['staged'] = j.get('staged') if isinstance(j.get('staged'), list) else []
        return j
    except (OSError, ValueError, TypeError):
        return None


def _exact_name_exists(path):
    """Windows 大小写不敏感；判断“实际显示名”是否已经是目标拼写。"""
    try:
        return os.path.basename(path) in os.listdir(os.path.dirname(path))
    except OSError:
        return False


def _ref_field(kind):
    return {'move': 'rolled_back', 'orphan': 'restored', 'staged': 'recovered'}[kind]


def _resolve_ref(j, plan):
    try:
        if plan['kind'] == 'staged':
            return j.get('staged', [])[int(plan['stage_index'])]
        return j.get('moves', [])[int(plan['move_index'])]
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _mark_ref(j, plan, success):
    ref = _resolve_ref(j, plan)
    if ref is None:
        return
    ref[_ref_field(plan['kind'])] = bool(success)
    if plan['kind'] == 'orphan' and success:
        # execute 写入的 staged 孤儿条目与 failed move 指向同一临时文件。
        for s in j.get('staged') or []:
            if os.path.normcase(s.get('tmp', '')) == os.path.normcase(plan.get('tmp', '')):
                s['recovered'] = True


def _rollback_complete(j):
    ok_moves = [m for m in (j.get('moves') or []) if m.get('ok')]
    failed_tmps = {os.path.normcase(m.get('tmp', '')) for m in (j.get('moves') or [])
                   if not m.get('ok')}
    staged = [s for s in (j.get('staged') or [])
              if os.path.normcase(s.get('tmp', '')) not in failed_tmps]
    orphans = [m for m in (j.get('moves') or [])
               if not m.get('ok') and m.get('tmp')]
    records = bool(ok_moves or staged or orphans)
    return (records
            and all(m.get('rolled_back') for m in ok_moves)
            and all(s.get('recovered') for s in staged)
            and all(m.get('restored') for m in orphans))


def rollback(jid):
    """串行化回滚入口。"""
    with _OP_LOCK:
        return _rollback_impl(jid)


def _rollback_impl(jid):
    """
    按日志反向恢复，支持：
    1. 普通改名（A<->B 交换、A->B->C 链式）；
    2. 部分成功后只处理剩余条目；
    3. execute 中途崩溃的 staged 计划；
    4. rollback 自己中途崩溃（rb_staged 计划先落盘）。
    所有当前路径都必须通过文件身份指纹校验，绝不把旁人搬进原名。
    """
    j = get_journal(jid)
    if not j:
        raise ValueError('日志不存在')
    if j.get('rolled_back'):
        raise ValueError('该次改名已经回滚过')

    failed = []
    skipped = []
    entries = []
    resume_plan = j.get('rb_staged') or []

    if resume_plan:
        for plan in resume_plan:
            ref = _resolve_ref(j, plan)
            if ref is None:
                failed.append({'src': plan.get('back', ''),
                               'reason': '日志记录索引损坏，无法确认回滚状态'})
                continue
            fp, cur, back, tmp = plan.get('fp'), plan.get('cur'), plan.get('back'), plan.get('tmp')
            idx = {'move_index': plan.get('move_index'),
                   'stage_index': plan.get('stage_index')}
            if os.path.exists(tmp) and _same_identity(fp, tmp):
                entries.append({'cur': tmp, 'back': back, 'fp': fp, 'ref': ref,
                                'kind': plan['kind'], 'plan': plan, 'rb_tmp': tmp, **idx})
            elif (os.path.exists(back) and _same_identity(fp, back)
                  and not os.path.exists(tmp)
                  and (os.path.normcase(back) != os.path.normcase(cur)
                       or _exact_name_exists(back))):
                _mark_ref(j, plan, True)
            elif os.path.exists(cur) and _same_identity(fp, cur):
                entries.append({'cur': cur, 'back': back, 'fp': fp, 'ref': ref,
                                'kind': plan['kind'], 'plan': plan, **idx})
            else:
                skipped.append({'path': back, 'reason': '文件身份不匹配或路径已不存在'})
    else:
        known_failed_tmps = set()
        for mi, m in enumerate(j.get('moves') or []):
            fp = m.get('fp')
            if m.get('ok') and not m.get('rolled_back'):
                cur, back = m.get('dst'), m.get('src')
                if (not os.path.exists(cur) and os.path.exists(back)
                        and _same_identity(fp, back)
                        and (os.path.normcase(cur) != os.path.normcase(back)
                             or _exact_name_exists(back))):
                    # 用户已手工改回原名：直接结清该条，不反复报“文件已不在”。
                    m['rolled_back'] = True
                    continue
                if not os.path.exists(cur):
                    skipped.append({'path': cur, 'reason': '文件已不在原改名位置'})
                elif not _same_identity(fp, cur):
                    skipped.append({'path': cur, 'reason': '该位置的文件已不是本次改名的文件'})
                else:
                    entries.append({'cur': cur, 'back': back, 'fp': fp, 'ref': m,
                                    'kind': 'move', 'move_index': mi})
            elif not m.get('ok') and m.get('tmp'):
                tmp = m['tmp']
                known_failed_tmps.add(os.path.normcase(tmp))
                if m.get('restored'):
                    continue
                if os.path.exists(tmp) and _same_identity(fp, tmp):
                    entries.append({'cur': tmp, 'back': m.get('src'), 'fp': fp, 'ref': m,
                                    'kind': 'orphan', 'move_index': mi})
                elif os.path.exists(tmp):
                    skipped.append({'path': tmp, 'reason': '临时名位置已被其他文件占用'})
                elif os.path.exists(m.get('src')) and _same_identity(fp, m.get('src')):
                    m['restored'] = True
                else:
                    skipped.append({'path': tmp, 'reason': '临时文件已不存在'})

        for si, s in enumerate(j.get('staged') or []):
            if s.get('recovered') or os.path.normcase(s.get('tmp', '')) in known_failed_tmps:
                continue
            src, tmp, dst, fp = s.get('src'), s.get('tmp'), s.get('dst'), s.get('fp')
            if (os.path.exists(src) and _same_identity(fp, src)
                    and (os.path.normcase(src) != os.path.normcase(dst)
                         or _exact_name_exists(src))):
                s['recovered'] = True
            elif os.path.exists(tmp) and _same_identity(fp, tmp):
                entries.append({'cur': tmp, 'back': src, 'fp': fp, 'ref': s,
                                'kind': 'staged', 'stage_index': si})
            elif (os.path.exists(dst) and _same_identity(fp, dst)
                  and _exact_name_exists(dst)):
                entries.append({'cur': dst, 'back': src, 'fp': fp, 'ref': s,
                                'kind': 'staged', 'stage_index': si})
            elif os.path.exists(src) or os.path.exists(tmp) or os.path.exists(dst):
                skipped.append({'path': src, 'reason': '文件身份不匹配，拒绝自动搬动'})
            else:
                skipped.append({'path': src, 'reason': '源文件、临时文件和目标文件均不存在'})

    # 交换/链式改名时，back 可能正被本批另一个 cur 占用；只有本批之外才跳过。
    current_set = {os.path.normcase(os.path.abspath(e['cur'])) for e in entries}
    todo = []
    for e in entries:
        back_key = os.path.normcase(os.path.abspath(e['back']))
        if os.path.exists(e['back']) and back_key not in current_set:
            skipped.append({'path': e['back'], 'reason': '原路径已被本批之外的文件占用'})
        else:
            todo.append(e)

    # 回滚计划也在第一次 rename 前落盘，解决“回滚中断后临时名无人知晓”。
    if todo:
        rb_plan = []
        for e in todo:
            if not e.get('rb_tmp'):
                e['rb_tmp'] = _temp_path(e['cur'], 'rb')
            plan = {
                'cur': e['cur'], 'back': e['back'], 'tmp': e['rb_tmp'],
                'fp': e.get('fp'), 'kind': e['kind'],
            }
            if e['kind'] == 'staged':
                plan['stage_index'] = e['stage_index']
            else:
                plan['move_index'] = e['move_index']
            e['plan'] = plan
            rb_plan.append(plan)
        j['rb_staged'] = rb_plan
        _write_journal(j)

    active = []
    for e in todo:
        if e.get('rb_tmp') and os.path.exists(e['rb_tmp']):
            active.append(e)             # 恢复上次已完成的第一次 rename
            continue
        if not os.path.exists(e['cur']) or not _same_identity(e.get('fp'), e['cur']):
            failed.append({'src': e['cur'], 'reason': '源文件身份已变化'})
            continue
        try:
            os.rename(e['cur'], e['rb_tmp'])
            active.append(e)
        except OSError as ex:
            failed.append({'src': e['cur'], 'reason': _err(ex)})

    done = 0
    for e in active:
        try:
            os.rename(e['rb_tmp'], e['back'])
            _mark_ref(j, e['plan'], True)
            done += 1
        except OSError as ex:
            restored = False
            try:
                os.rename(e['rb_tmp'], e['cur'])
                restored = True
            except OSError:
                pass
            failed.append({
                'src': e['back'],
                'reason': _err(ex) + ('' if restored else '（文件停在回滚临时名：%s）' % e['rb_tmp']),
            })

    # 本次已处理的条目不再需要恢复计划；只有真正停在 .~rb~ 的文件才保留计划。
    if not any(e.get('rb_tmp') and os.path.exists(e['rb_tmp']) for e in todo):
        j['rb_staged'] = []

    complete = _rollback_complete(j)
    ok = bool(complete) and not failed and not skipped
    j['rolled_back'] = ok
    j['last_rollback_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    if ok:
        j['rolled_back_at'] = j['last_rollback_at']
        j['status'] = 'recovered'
        j['staged'] = []
        j['rb_staged'] = []
    j['rollback_done'] = done
    j['rollback_failed'] = failed
    j['rollback_skipped'] = skipped
    warning = ''
    try:
        _write_journal(j)
    except OSError as e:
        warning = '回滚已完成，但日志收尾写入失败：%s' % e

    result = {'ok': ok, 'done': done, 'skipped': skipped, 'failed': failed}
    if warning:
        result['warning'] = warning
    if not result['ok']:
        if not todo and not skipped and not failed:
            result['error'] = '没有可回滚的记录'
        else:
            result['error'] = '回滚未全部完成：成功 %d，跳过 %d，失败 %d' % (
                done, len(skipped), len(failed))
    return result
