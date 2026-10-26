# -*- coding: utf-8 -*-
"""派活档位与 Claude 额度：配置解析、生效档位、会话状态、命令行子命令。

被 hooks/dispatch_hook.py 当库导入，也可以直接跑（Claude 在会话里调用）：
  python dispatch_policy.py status
  python dispatch_policy.py decide dispatch|self --reason "..." [--files a.go,b.go] [--minutes 30]
  python dispatch_policy.py override --level 3      # 只对本会话有效
  python dispatch_policy.py override --clear
  python dispatch_policy.py record-codex --mode read
  python dispatch_policy.py record-codex-from-git --root <项目根>   # 写模式派活跑完后统计改动
  python dispatch_policy.py check-protected --root <项目根> --globs "tests/acceptance/**"
  python dispatch_policy.py init-user [--budget ample|tight] [--default-level N]
  python dispatch_policy.py set-project-level N

规则与理由见 docs/levels.md。只用标准库：钩子每次工具调用都会起一个 python，不能慢。
"""
import argparse
import contextlib
import datetime as _dt
import difflib
import fnmatch
import json
import os
import subprocess
import sys
import time

LEVEL_NAMES = {0: "关", 1: "轻", 2: "中", 3: "重"}
BUDGETS = ("ample", "tight")
BUDGET_NAMES = {"ample": "宽裕", "tight": "紧"}
DEFAULT_LEVEL = 1
DEFAULT_BUDGET = "ample"

# 「源码」的默认口径：可在项目配置里用 source_exts / exclude 覆盖
DEFAULT_SOURCE_EXTS = (
    ".go", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".rs", ".java", ".kt", ".cs", ".c", ".h", ".cpp", ".hpp", ".rb", ".php", ".swift",
    ".sql", ".ps1", ".sh", ".bash", ".css", ".scss", ".less", ".html",
)
DEFAULT_EXCLUDE_DIRS = (
    "node_modules", "dist", "build", "target", ".git", ".venv", "venv", "__pycache__",
    ".claude", "vendor", "coverage", ".next", "out", "docs",
)
# 派活日志本身会被 decide 写入；它不算任何一方的改动，也不该让写模式派活因「工作树不干净」被拒
LOG_REL = "docs/dispatch-log.md"

ANALYSIS_REMIND_FIRST = 2      # 第 2 个分析脚本起提醒
ANALYSIS_REMIND_EVERY = 3      # 之后每多 3 个再提醒
SRC_REMIND_FILES = 3           # 档位 2：自上次登记起 Claude 改了 3 个源码文件……
SRC_REMIND_LINES = 80          # ……或 80 行
WAIVER_MINUTES = 30            # 档位 3：登记「自己做」后的豁免时长
SESSION_MATCH_HOURS = 12       # CLI 找「当前会话」时只看最近这么久更新过的状态文件


# ---------------------------------------------------------------- 路径

def base_dir():
    """用户级配置与状态的根。测试用 CODEX_DISPATCH_HOME 隔离。"""
    return os.environ.get("CODEX_DISPATCH_HOME") or os.path.join(os.path.expanduser("~"), ".claude")


def user_cfg_path():
    return os.path.join(base_dir(), "codex-dispatch.json")


def state_dir():
    d = os.path.join(base_dir(), "codex-dispatch-state")
    os.makedirs(os.path.join(d, "sessions"), exist_ok=True)
    return d


def norm(p):
    return os.path.normcase(os.path.abspath(p))


def file_key(rel):
    """统计、比对用的文件键。Windows 文件系统不分大小写：src/A.py 和 SRC/a.py 是同一个文件。"""
    return rel.lower() if os.name == "nt" else rel


def is_log(rel):
    return file_key(rel) == file_key(LOG_REL)


def project_root(cwd):
    """cwd 所在 git 仓库的根（.git 是目录或文件都算）；不在仓库里就返回 cwd。"""
    cur = os.path.abspath(cwd or os.getcwd())
    probe = cur
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return cur
        probe = parent


def project_cfg_path(root):
    return os.path.join(root, ".claude", "codex-dispatch.json")


# ---------------------------------------------------------------- 配置

def _load_json(path):
    """返回 (数据, 错误说明)。文件不存在不算错误。"""
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001 —— 任何解析失败都按「配置无效」处理
        return None, "%s 解析失败：%s" % (path, e)
    if not isinstance(data, dict):
        return None, "%s 不是 JSON 对象" % path
    return data, None


def _valid_level(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 3


def effective_level(level, budget):
    if budget == "tight":
        return min(3, max(level + 1, 2))
    return level


def _write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _overrides_path():
    return os.path.join(state_dir(), "overrides.json")


def load_overrides():
    data, _ = _load_json(_overrides_path())
    items = (data or {}).get("items", [])
    now = time.time()
    # 24 小时没绑定或没清理的覆盖视为残留
    return [o for o in items if now - o.get("created", 0) < 86400]


def save_overrides(items):
    _write_json_atomic(_overrides_path(), {"items": items})


def find_override(root, session_id, bind=False):
    """本会话的临时覆盖。未绑定的覆盖由该项目下第一个看到它的会话认领。"""
    items = load_overrides()
    nroot = norm(root)
    hit = None
    for o in items:
        if norm(o.get("project", "")) != nroot:
            continue
        if o.get("session_id") == session_id and session_id:
            hit = o
            break
        if o.get("session_id") is None and hit is None:
            hit = o
    if hit and bind and session_id and hit.get("session_id") is None:
        hit["session_id"] = session_id
        save_overrides(items)
    return hit


def resolve(cwd, session_id=None, bind=False):
    """算出本次生效的档位。永不抛异常：配置有问题就带着警告回到默认值。"""
    root = project_root(cwd)
    warnings = []
    ucfg, err = _load_json(user_cfg_path())
    if err:
        warnings.append(err)
    ucfg = ucfg or {}
    budget = ucfg.get("claude_budget", DEFAULT_BUDGET)
    if budget not in BUDGETS:
        warnings.append("claude_budget=%r 无效（应为 ample/tight），按 %s 处理" % (budget, DEFAULT_BUDGET))
        budget = DEFAULT_BUDGET
    default_level = ucfg.get("default_level", DEFAULT_LEVEL)
    if not _valid_level(default_level):
        warnings.append("default_level=%r 无效（应为 0~3），按 %d 处理" % (default_level, DEFAULT_LEVEL))
        default_level = DEFAULT_LEVEL

    pcfg, err = _load_json(project_cfg_path(root))
    if err:
        warnings.append(err)
    pcfg = pcfg or {}
    level, source = default_level, "用户默认"
    if "level" in pcfg:
        if _valid_level(pcfg["level"]):
            level, source = pcfg["level"], "项目"
        else:
            warnings.append("项目 level=%r 无效（应为 0~3），按默认 %d 处理" % (pcfg["level"], default_level))

    project_level = level
    ov = find_override(root, session_id, bind=bind)
    if ov and _valid_level(ov.get("level")):
        level, source = ov["level"], "本会话临时覆盖"

    exts = pcfg.get("source_exts")
    if not (isinstance(exts, list) and all(isinstance(e, str) for e in exts)):
        exts = list(DEFAULT_SOURCE_EXTS)
    excl = pcfg.get("exclude")
    if not (isinstance(excl, list) and all(isinstance(e, str) for e in excl)):
        excl = list(DEFAULT_EXCLUDE_DIRS)

    return {
        "root": root,
        "project_level": project_level,
        "level": level,
        "level_source": source,
        "budget": budget,
        "effective": effective_level(level, budget),
        "warnings": warnings,
        "source_exts": [e.lower() for e in exts],
        "exclude": excl,
    }


def summary_line(r):
    eff = r["effective"]
    s = "派活档位：%s %d · Claude 额度 %s → 生效 %d（%s）" % (
        r["level_source"], r["level"], BUDGET_NAMES[r["budget"]], eff, LEVEL_NAMES[eff])
    if r["warnings"]:
        s += " ｜配置问题：" + "；".join(r["warnings"])
    return s


# ---------------------------------------------------------------- 源码判定

def rel_in_root(path, root):
    """path 在 root 内则返回 posix 风格相对路径，否则 None。"""
    if not path:
        return None
    ap = path if os.path.isabs(path) else os.path.join(root, path)
    np_, nr = norm(ap), norm(root)
    if np_ != nr and not np_.startswith(nr.rstrip("\\/") + os.sep):
        return None
    return os.path.relpath(os.path.abspath(ap), os.path.abspath(root)).replace("\\", "/")


def is_source(path, r):
    rel = rel_in_root(path, r["root"])
    if rel is None or is_log(rel):
        return False
    parts = rel.split("/")
    excl = {e.lower() for e in r["exclude"]}
    if any(p.lower() in excl for p in parts[:-1]):
        return False
    return os.path.splitext(parts[-1])[1].lower() in r["source_exts"]


# ---------------------------------------------------------------- 命令行改动（git 快照）

CODEX_RUNNING_MAX = 2 * 3600   # 后台派活标记的最长有效期：Codex 崩了没走到统计那步时自动失效


def dirty_sources(r):
    """项目里当前有改动的源码文件 {file_key(相对路径): mtime}（含未跟踪）。不是 git 仓库或 git 失败返回 None。

    用来发现「不经编辑工具」改出来的源码：Claude Code 给 Bash 钩子的结果里没有改动文件列表（10-26 实测），
    只能前后对比。"""
    root = r["root"]
    if not os.path.exists(os.path.join(root, ".git")):
        return None
    try:
        out = subprocess.run(["git", "-C", root, "status", "--porcelain", "-z", "--untracked-files=all"],
                             capture_output=True, timeout=5)
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0:
        return None
    toks = out.stdout.decode("utf-8", "replace").split("\0")
    res = {}
    i = 0
    while i < len(toks):
        t = toks[i]
        i += 1
        if len(t) < 4:
            continue
        xy, path = t[:2], t[3:]
        if xy[0] in "RC":        # 改名/复制：后面多一个原路径
            i += 1
        if "D" in xy:
            continue
        full = os.path.join(root, path)
        if not is_source(full, r):
            continue
        try:
            res[file_key(path)] = os.path.getmtime(full)
        except OSError:
            continue
    return res


def codex_running(st):
    return time.time() < st.get("codex_running_until", 0)


# ---------------------------------------------------------------- 行数统计

def _lines(s):
    return (s or "").splitlines()


def diff_counts(old, new):
    """old→new 的增删行数（按行比对，不把未改的上下文算进去）。"""
    a, b = _lines(old), _lines(new)
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def counts_from_patch(resp):
    """Claude Code 的 Edit/Write 结果里若带 structuredPatch，用它算最准。"""
    patch = resp.get("structuredPatch") if isinstance(resp, dict) else None
    if not isinstance(patch, list) or not patch:
        return None
    added = removed = 0
    for h in patch:
        for ln in (h.get("lines") or []) if isinstance(h, dict) else []:
            if isinstance(ln, str) and ln.startswith("+"):
                added += 1
            elif isinstance(ln, str) and ln.startswith("-"):
                removed += 1
    return added, removed


def edit_counts(tool, tin, resp):
    got = counts_from_patch(resp)
    if got is not None:
        return got
    tin = tin or {}
    if tool == "Edit":
        return diff_counts(tin.get("old_string"), tin.get("new_string"))
    if tool == "MultiEdit":
        a = r = 0
        for e in tin.get("edits") or []:
            x, y = diff_counts(e.get("old_string"), e.get("new_string"))
            a, r = a + x, r + y
        return a, r
    if tool == "Write":
        return len(_lines(tin.get("content"))), 0
    if tool == "NotebookEdit":
        return len(_lines(tin.get("new_source"))), 0
    return 0, 0


# ---------------------------------------------------------------- 会话状态

def _session_path(sid):
    safe = "".join(c for c in (sid or "nosession") if c.isalnum() or c in "-_")
    return os.path.join(state_dir(), "sessions", safe + ".json")


def new_state(sid, root):
    return {
        "session_id": sid, "project": root, "created": time.time(), "updated": time.time(),
        "claude": {"files": {}, "shell_files": []},
        "analysis_scripts": 0, "analysis_next_remind": ANALYSIS_REMIND_FIRST,
        "since_decision": {"files": [], "lines": 0, "reminded_at_files": 0},
        "decisions": [], "waivers": [], "violations": [],
        "codex": {"runs": 0, "read_runs": 0, "write_runs": 0, "added": 0, "removed": 0, "files": 0},
        "last_stop": None,
    }


def load_state(sid, root):
    data, _ = _load_json(_session_path(sid))
    if not data:
        return new_state(sid, root)
    base = new_state(sid, root)
    base.update(data)
    return base


def save_state(st):
    st["updated"] = time.time()
    _write_json_atomic(_session_path(st["session_id"]), st)


@contextlib.contextmanager
def _locked(sid, wait=3.0, stale=10.0):
    """会话状态的读改写锁。同一会话的工具调用可能并行，各自的钩子会同时读写同一份状态。
    拿不到锁（超时）也照样往下走：宁可偶尔丢一条统计，也不能让钩子卡住工具调用。"""
    lock = _session_path(sid) + ".lock"
    deadline = time.time() + wait
    fd = None
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > stale:   # 持锁进程崩了留下的残留
                    os.remove(lock)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                break
            time.sleep(0.02)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.remove(lock)
            except OSError:
                pass


@contextlib.contextmanager
def session(sid, root):
    """加锁读出会话状态，块结束时写回。"""
    with _locked(sid):
        st = load_state(sid, root)
        yield st
        save_state(st)


def current_session_state(root):
    """CLI 没有 session_id：取该项目最近更新的会话状态。"""
    d = os.path.join(state_dir(), "sessions")
    best, best_t = None, 0
    nroot = norm(root)
    for fn in os.listdir(d):
        if not fn.endswith(".json"):
            continue
        data, _ = _load_json(os.path.join(d, fn))
        if not data or norm(data.get("project", "")) != nroot:
            continue
        t = data.get("updated", 0)
        if t > best_t and time.time() - t < SESSION_MATCH_HOURS * 3600:
            best, best_t = data, t
    if best is None:
        return None
    st = new_state(best["session_id"], root)
    st.update(best)
    return st


def _same_file(path, f, root):
    """登记时的文件可能写成绝对路径、相对项目根的路径，或只写文件名。
    带目录的必须精确匹配（src/a.py 不放行 other/src/a.py）；只写文件名时才按文件名匹配。"""
    if os.path.isabs(f):
        return norm(path) == norm(f)
    rel = rel_in_root(path, root)
    if rel is None:
        return False
    want = file_key(f.replace("\\", "/").strip("/"))
    if "/" in want:
        return file_key(rel) == want
    return file_key(rel.rsplit("/", 1)[-1]) == want


def active_waiver(st, path):
    now = time.time()
    for w in st.get("waivers", []):
        if w.get("until", 0) < now:
            continue
        files = w.get("files")
        if not files:
            return w
        if any(_same_file(path, f, st["project"]) for f in files):
            return w
    return None


def session_totals(st):
    files = st["claude"]["files"]
    return {
        "claude_files": len(files),
        "claude_added": sum(v.get("added", 0) for v in files.values()),
        "claude_removed": sum(v.get("removed", 0) for v in files.values()),
        "shell_files": len(st["claude"].get("shell_files", [])),
        "codex_runs": st["codex"]["runs"],
        "codex_added": st["codex"]["added"],
        "codex_removed": st["codex"]["removed"],
        "analysis_scripts": st.get("analysis_scripts", 0),
    }


def totals_line(st, r):
    t = session_totals(st)
    s = "本会话：Claude 改 %d 文件 +%d/-%d" % (t["claude_files"], t["claude_added"], t["claude_removed"])
    if t["shell_files"]:
        s += " · 命令行改 %d 文件" % t["shell_files"]
    s += " · Codex 派活 %d 次 +%d/-%d" % (t["codex_runs"], t["codex_added"], t["codex_removed"])
    s += " · 生效档位 %d" % r["effective"]
    return s


# ---------------------------------------------------------------- 派活日志

def append_log(root, title, lines):
    path = os.path.join(root, *LOG_REL.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("# 派活日志\n\n每次验收完记一条；考虑过但决定自己做的也记。带「自动」的是 dispatch_policy.py 登记的。\n")
        f.write("\n## %s · %s\n" % (stamp, title))
        for ln in lines:
            f.write("- %s\n" % ln)
    return path


def append_stats(record):
    with open(os.path.join(state_dir(), "stats.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- git 统计（写模式派活之后）

def _git(root, *args):
    out = subprocess.run(["git", "-C", root] + list(args), capture_output=True)
    return out.stdout.decode("utf-8", "replace")


def git_changes(root):
    """当前工作树相对 HEAD 的改动：(增, 删, 文件数, 改动文件列表)，排除派活日志。
    用 -z：默认输出会给中文等非 ASCII 文件名加引号转义，按名字打开就失败、漏算。"""
    added = removed = 0
    files = []
    toks = _git(root, "diff", "--numstat", "-z", "HEAD").split("\0")
    i = 0
    while i < len(toks):
        parts = toks[i].split("\t")
        i += 1
        if len(parts) < 3:
            continue
        path = parts[2]
        if path == "" and i + 1 < len(toks):    # 改名：路径为空，后面跟「旧\0新」
            path = toks[i + 1]
            i += 2
        if not path or is_log(path):
            continue
        files.append(path)
        if parts[0].isdigit():
            added += int(parts[0])
        if parts[1].isdigit():
            removed += int(parts[1])
    for rel in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if not rel or is_log(rel):
            continue
        files.append(rel)
        try:
            with open(os.path.join(root, rel), "rb") as f:
                added += f.read().count(b"\n")
        except OSError:
            pass
    return added, removed, len(files), files


def match_any(rel, globs):
    return any(fnmatch.fnmatch(rel, g) for g in globs)


# ---------------------------------------------------------------- CLI

def _say(msg):
    sys.stdout.write(msg + "\n")


def cmd_status(a):
    r = resolve(a.cwd)
    _say(summary_line(r))
    _say("项目根：%s" % r["root"])
    st = current_session_state(r["root"])
    if st:
        _say(totals_line(st, r))
        w = [x for x in st.get("waivers", []) if x.get("until", 0) > time.time()]
        if w:
            _say("有效豁免：%d 条（最早到 %s）" % (len(w), time.strftime("%H:%M", time.localtime(min(x["until"] for x in w)))))
    return 0


def _cli_sid(a, root):
    if getattr(a, "session", None):
        return a.session
    st = current_session_state(root)
    return st["session_id"] if st else "cli-%d" % os.getpid()


def cmd_decide(a):
    r = resolve(a.cwd, getattr(a, "session", None))
    sid = _cli_sid(a, r["root"])
    files = [f.strip() for f in (a.files or "").split(",") if f.strip()]
    now = time.time()
    rec = {"time": now, "choice": a.choice, "reason": a.reason, "files": files, "effective": r["effective"]}
    with session(sid, r["root"]) as st:
        st["decisions"].append(rec)
        st["since_decision"] = {"files": [], "lines": 0, "reminded_at_files": 0}
        if a.choice == "self":
            st["waivers"].append({"until": now + a.minutes * 60, "files": files or None, "reason": a.reason})
    title = "登记：%s（自动）" % ("派给 Codex" if a.choice == "dispatch" else "自己做")
    lines = ["档位：生效 %d（%s %d · 额度 %s）" % (r["effective"], r["level_source"], r["level"], BUDGET_NAMES[r["budget"]]),
             "理由：%s" % a.reason]
    if files:
        lines.append("涉及文件：%s" % ", ".join(files))
    path = append_log(r["root"], title, lines)
    _say("已登记（%s），写入 %s" % ("派给 Codex" if a.choice == "dispatch" else "自己做", path))
    if a.choice == "self" and r["effective"] >= 3:
        _say("档位 3 豁免 %d 分钟%s" % (a.minutes, "，限这些文件" if files else ""))
    return 0


def cmd_override(a):
    root = project_root(a.cwd)
    sid = a.session or (current_session_state(root) or {}).get("session_id")
    # 去掉本会话已有的、以及该项目未认领的覆盖；其他会话认领的不动
    items = [o for o in load_overrides()
             if norm(o.get("project", "")) != norm(root) or o.get("session_id") not in (None, sid)]
    if a.clear:
        save_overrides(items)
        _say("已清除本会话在 %s 的临时覆盖" % root)
        return 0
    if not _valid_level(a.level):
        _say("level 必须是 0~3")
        return 2
    items.append({"project": root, "level": a.level, "created": time.time(), "session_id": sid})
    save_overrides(items)
    _say("本会话临时按档位 %d（%s）；会话结束自动失效" % (a.level, LEVEL_NAMES[a.level]))
    return 0


def cmd_record_codex(a, added=0, removed=0, nfiles=0):
    root = project_root(a.cwd if getattr(a, "cwd", None) else a.root)
    sid = _cli_sid(a, root)
    with session(sid, root) as st:
        st["codex_running_until"] = 0
        snap = dirty_sources(resolve(root, sid))
        if snap is not None:
            st["git_snapshot"] = snap   # Codex 的改动不算到 Claude 的命令行改动里
        c = st["codex"]
        c["runs"] += 1
        c["read_runs" if a.mode == "read" else "write_runs"] += 1
        c["added"] += added
        c["removed"] += removed
        c["files"] += nfiles
    append_stats({"time": time.time(), "kind": "codex_run", "project": root, "mode": a.mode,
                  "added": added, "removed": removed, "files": nfiles, "session_id": sid})
    return 0


def cmd_record_codex_from_git(a):
    added, removed, nfiles, _ = git_changes(a.root)
    a.mode = "write"
    cmd_record_codex(a, added, removed, nfiles)
    _say("Codex 改动：%d 文件 +%d/-%d（已计入本会话统计）" % (nfiles, added, removed))
    return 0


def cmd_check_protected(a):
    globs = [g.strip() for g in a.globs.split(",") if g.strip()]
    _, _, _, files = git_changes(a.root)
    bad = [f for f in files if match_any(f, globs)]
    for f in bad:
        _say(f)
    return 1 if bad else 0


def cmd_init_user(a):
    path = user_cfg_path()
    data, _ = _load_json(path)
    data = data or {}
    if a.budget:
        data["claude_budget"] = a.budget
    if a.default_level is not None:
        data["default_level"] = a.default_level
    data.setdefault("claude_budget", DEFAULT_BUDGET)
    data.setdefault("default_level", DEFAULT_LEVEL)
    _write_json_atomic(path, data)
    _say("已写 %s：%s" % (path, json.dumps(data, ensure_ascii=False)))
    return 0


def cmd_set_project_level(a):
    if not _valid_level(a.level):
        _say("level 必须是 0~3")
        return 2
    root = project_root(a.cwd)
    path = project_cfg_path(root)
    data, _ = _load_json(path)
    data = data or {}
    data["level"] = a.level
    _write_json_atomic(path, data)
    _say("已写 %s：level=%d（%s）" % (path, a.level, LEVEL_NAMES[a.level]))
    return 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="派活档位与 Claude 额度")
    p.add_argument("--cwd", default=os.getcwd())
    # 钩子注入的命令都带本会话 ID；不带时退回「该项目最近更新的会话」（同项目开两个会话会认错）
    p.add_argument("--session", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    d = sub.add_parser("decide")
    d.add_argument("choice", choices=["dispatch", "self"])
    d.add_argument("--reason", required=True)
    d.add_argument("--files", default="")
    d.add_argument("--minutes", type=int, default=WAIVER_MINUTES)
    o = sub.add_parser("override")
    o.add_argument("--level", type=int)
    o.add_argument("--clear", action="store_true")
    rc = sub.add_parser("record-codex")
    rc.add_argument("--mode", choices=["read", "write"], required=True)
    rg = sub.add_parser("record-codex-from-git")
    rg.add_argument("--root", required=True)
    cp = sub.add_parser("check-protected")
    cp.add_argument("--root", required=True)
    cp.add_argument("--globs", required=True)
    iu = sub.add_parser("init-user")
    iu.add_argument("--budget", choices=list(BUDGETS))
    iu.add_argument("--default-level", type=int)
    sp = sub.add_parser("set-project-level")
    sp.add_argument("level", type=int)
    a = p.parse_args(argv)
    if a.cmd == "record-codex-from-git":
        a.cwd = a.root
        return cmd_record_codex_from_git(a)
    return {
        "status": cmd_status, "decide": cmd_decide, "override": cmd_override,
        "record-codex": cmd_record_codex, "check-protected": cmd_check_protected,
        "init-user": cmd_init_user, "set-project-level": cmd_set_project_level,
    }[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
