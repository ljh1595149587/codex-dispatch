# -*- coding: utf-8 -*-
"""Claude Code 全局钩子入口：python dispatch_hook.py <事件>，钩子输入从 stdin 读 JSON。

事件：session-start / pre-tool / post-tool / stop / session-end。规则见 docs/levels.md。

纪律：任何异常都放行（exit 0、无输出）并记到 state 目录的 errors.log。
工具链自己出故障时不能卡住正常工作；唯一有意的拦截是档位 3 下 Claude 直接改源码。
"""
import json
import os
import re
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))
import dispatch_policy as dp  # noqa: E402

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
POLICY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "dispatch_policy.py")

# 内联分析程序：python -c / python - <<EOF / python3 <<EOF / awk '...'，程序够长才算「写了一个分析脚本」
_INLINE = re.compile(r"\b(python3?|py)\b[^\n|;&]*?(\s-c\s|\s-\s*<<|\s<<)|\bawk\s+'", re.I)
ANALYSIS_MIN_CHARS = 300
ANALYSIS_MIN_LINES = 8
# 编辑脚本（读文件→replace→写回、合并 JSON）不是分析：有写文件、没有统计 ⇒ 不计数。
# 已知局限（两轮审查认可、回放数据定的取舍）：统计只用 len( 的分析会被当编辑漏计——把 len( 算统计会让约 10% 的编辑脚本
# 重新误计；一条命令里分析段 + 编辑段整体判定；被替换的文本里恰好有 mean / sum( 时编辑仍会被计（与旧行为相同）
_OPEN_ARGS = r"""\bopen\((?:[^()\n]|\([^()\n]*\))*"""        # 参数里允许一层括号：open(Path(p), 'w')
_MODE = r"""['"](?=[rbt]*[wax+])[rwaxbt+]{1,4}['"]"""        # 'w' 'wb' 'wt' 'a' 'x' 'r+' 'w+'
_WRITES = re.compile(_OPEN_ARGS + r",\s*" + _MODE + r"|" + _OPEN_ARGS + r"\bmode\s*=\s*" + _MODE
                     + r"""|\.write_(text|bytes)\(|\bos\.(replace|rename)\(|\bshutil\.(move|copy\w*)\("""
                     r"""|\bjson\.dump\((?![^\n]*sys\.std)""")
_STATS = re.compile(r"\b(Counter|most_common|defaultdict|groupby|value_counts|statistics|mean|median"
                    r"|percentile|quantile|histogram)\b|\bsum\(|\+=\s*1\b")


def _out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))


def _ctx(event, text):
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _cmd(sub, sid=None):
    """给 Claude 的命令。带上本会话 ID：同一项目开着两个会话时，登记不会记到另一个会话上。"""
    sess = ' --session "%s"' % sid if sid else ""
    return 'python "%s"%s %s' % (POLICY.replace("\\", "/"), sess, sub)


def _rules(eff, budget, sid=None):
    lines = []
    if eff == 0:
        lines.append("档位 0：不主动派活；用户明确要求时才派。")
    if eff >= 1:
        lines.append("读多写少的排查、日志/数据统计：先问自己「这批能不能一次派给 Codex」；本会话第 2 个分析脚本起钩子会提醒。")
    if eff >= 2:
        lines.append("需求写得清、有测试能判对错的实现派给 Codex（codex-dispatch skill）；设计、模糊需求、核心逻辑自己做。"
                     "规划时 task_plan 每个阶段填 执行者 / 验收者 / 风险。Claude 改源码累计到阈值时必须登记："
                     + _cmd('decide dispatch|self --reason "..."', sid))
    if eff >= 3:
        lines.append("档位 3：Claude 直接改项目源码会被拦截。确需自己小修，先登记：" + _cmd('decide self --reason "..." --files a,b', sid)
                     + "（豁免 30 分钟）。")
    if budget == "tight":
        lines.append("Claude 额度紧：排查/读日志/统计一律派 Codex 只读档、报告 ≤4KB；改→测→修循环归 Codex；验收只看统计、关键片段和命令输出，深度审查走两轮审查。")
    if eff >= 1:
        lines.append("验收：实现者不能是唯一验收者；风险高（迁移/计费/鉴权/并发/部署）四层验收全做。用户说「这次按 N 档」时执行 "
                     + _cmd("override --level N", sid) + "。")
    return "\n".join("- " + x for x in lines)


def _debug_on():
    if os.environ.get("CODEX_DISPATCH_HOOK_DEBUG") == "1":
        return True
    data, _ = dp._load_json(dp.user_cfg_path())
    return bool((data or {}).get("debug"))


def _debug(event, inp):
    def trim(v, depth=0):
        if isinstance(v, str):
            return v[:400]
        if isinstance(v, dict) and depth < 4:
            return {k: trim(x, depth + 1) for k, x in list(v.items())[:40]}
        if isinstance(v, list) and depth < 4:
            return [trim(x, depth + 1) for x in v[:20]]
        return v if depth < 4 else "<…>"
    with open(os.path.join(dp.state_dir(), "debug.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "event": event, "input": trim(inp)}, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 项目根与快照
#
# 钩子输入的 cwd 跟着 Bash 的 cd 走。只按 cwd 找根的话，cd 到普通目录（不是 git 仓库）后，
# 本项目的文件全成了「项目外」：源码改动不记账、.py 被当分析脚本（10-26 实测）。所以：
#   编辑工具 → 文件所在的 git 仓库；命令 → cwd 所在的 git 仓库；都不在仓库里 → 会话起始项目。

def _session_root(inp, sid):
    return dp.session_project(sid) or dp.project_root(inp.get("cwd"))


def _pick_root(inp, sid, path=None):
    if path:
        base = path if os.path.isabs(path) else os.path.join(inp.get("cwd") or os.getcwd(), path)
        g = dp.git_root(os.path.dirname(base))
        if g:
            return g
    return dp.git_root(inp.get("cwd")) or _session_root(inp, sid)


def _snapshots(st):
    snaps = st.setdefault("git_snapshots", {})
    old = st.pop("git_snapshot", None)                  # 旧格式：只存一份，属于会话起始项目
    if isinstance(old, dict) and st.get("project"):
        snaps.setdefault(dp.norm(st["project"]), old)
    return snaps


def _acct_key(st, root, rel):
    """记账用的文件键。会话起始项目内是相对路径；别的仓库用「//仓库根/相对路径」——
    相对路径不会以 / 开头，两类键不会撞（只带仓库名会撞：起始项目的 b/x.go 与仓库 b 的 x.go）。"""
    if not st.get("project") or dp.norm(root) == dp.norm(st["project"]):
        return dp.file_key(rel)
    return dp.file_key("//" + os.path.abspath(root).replace("\\", "/").strip("/") + "/" + rel)


# ---------------------------------------------------------------- 事件

def on_session_start(inp):
    sid = inp.get("session_id")
    r = dp.resolve(inp.get("cwd"), sid, bind=True)
    with dp.session(sid, r["root"]) as st:
        snaps = _snapshots(st)
        if r["effective"] >= 1 and dp.norm(r["root"]) not in snaps:
            snaps[dp.norm(r["root"])] = dp.dirty_sources(r)   # 基线：会话开始前已有的改动不算 Claude 的
    line = dp.summary_line(r)
    text = "[codex-dispatch] %s\n项目根：%s\n%s" % (line, r["root"], _rules(r["effective"], r["budget"], sid))
    out = _ctx("SessionStart", text)
    out["systemMessage"] = line
    _out(out)


def on_pre_tool(inp):
    tool = inp.get("tool_name")
    if tool not in EDIT_TOOLS:
        return
    sid = inp.get("session_id")
    tin = inp.get("tool_input") or {}
    path = tin.get("file_path") or tin.get("notebook_path")
    r = dp.resolve(inp.get("cwd"), sid, bind=True, root=_pick_root(inp, sid, path))
    if r["effective"] < 3:
        return
    if not dp.is_source(path, r):
        return
    with dp.session(sid, r["root"]) as st:
        if dp.active_waiver(st, path):
            return
        st["violations"].append({"time": time.time(), "kind": "blocked_edit", "file": path})
    # 豁免按会话起始项目解析相对路径：文件在别的仓库时给绝对路径，否则登记了也放不行、还会误放起始项目的同名文件
    home = st.get("project")
    if home and dp.norm(home) != dp.norm(r["root"]):
        wf = '"%s"' % os.path.abspath(path)
    else:
        wf = dp.rel_in_root(path, r["root"]) or os.path.basename(path)
    reason = ("[codex-dispatch] 生效档位 3：项目源码由 Codex 实现，Claude 不直接改。"
              "请把这项改动写成需求派给 Codex（codex-dispatch skill，写模式）。"
              "确属必须自己做的小修，先登记理由再改：" + _cmd('decide self --reason "..." --files ' + wf, sid))
    _out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                 "permissionDecisionReason": reason}})


def _bash_changed_files(resp):
    """从 Bash 的 tool_response 里找「这条命令改了哪些文件」。字段名随版本可能不同，宽松地找路径列表。"""
    found = []
    if not isinstance(resp, dict):
        return found
    for key, val in resp.items():
        if not re.search(r"(changed|modified|edited|written|touched).*files?|files?.*(changed|modified)|^diff", key, re.I):
            continue
        items = val if isinstance(val, list) else (val.get("files") if isinstance(val, dict) else None)
        for it in items or []:
            if isinstance(it, str):
                found.append(it)
            elif isinstance(it, dict):
                p = it.get("path") or it.get("filePath") or it.get("file_path") or it.get("file")
                if isinstance(p, str):
                    found.append(p)
    return found


def _is_edit_script(code):
    return isinstance(code, str) and _WRITES.search(code) is not None and _STATS.search(code) is None


# 只认「像路径」的片段：从开头 / 空白 / 引号 / = ( , 之后起，每段只有文件名字符。
# 不能宽到「含 / 的连续非空白」——不带空格的 awk 统计程序（含 sx/n 这类除法）会被整段压掉（两轮审查 10-26）
_PATHISH = re.compile(r"""(?:(?<=[\s"'=(,])|^)(?:[A-Za-z]:|~|\.{1,2})?(?:[/\\][\w.\-~%+@]+)+[/\\]?""")


def _program_len(cmd):
    """量程序有多长时，路径只按一个字符算：scratchpad 路径动辄 100+ 字符，一行 awk 也会被撑过门槛（10-26 实测）。"""
    return len(_PATHISH.sub(lambda m: "P" if len(m.group(0)) > 30 else m.group(0), cmd))


def _is_analysis_command(cmd):
    if not cmd or not _INLINE.search(cmd) or _is_edit_script(cmd):
        return False
    return _program_len(cmd) >= ANALYSIS_MIN_CHARS or cmd.count("\n") + 1 >= ANALYSIS_MIN_LINES


def _is_analysis_file(path, r):
    if not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".py", ".sh", ".ps1"):
        return False
    # scratchpad、临时目录里写的脚本 = 一次性分析脚本；项目内写脚本算源码改动。
    # 项目外但不在临时目录（cwd 漂到别处后改本仓库、改别的仓库）不算：那是改代码，不是写分析脚本
    rel = dp.rel_in_root(path, r["root"])
    # 相对路径和 rel_in_root 一样按项目根解析，不按钩子进程的 cwd
    where = rel if rel is not None else os.path.abspath(os.path.join(r["root"], path)).replace("\\", "/")
    return re.search(r"(^|/)(scratch|scratchpad|tmp|temp)/", where, re.I) is not None


# 走 codex MCP 派的活：codex = 开新线程（一次派活），codex-reply = 同一线程续聊
_CODEX_MCP = re.compile(r"^mcp__.*codex.*__codex(-reply)?$")


def _thread_of(inp, sid):
    """续聊对应的线程记录（开线程时记下的仓库与档）。只读、不加锁：线程记录写入后不再改。"""
    tin = inp.get("tool_input") or {}
    tid = tin.get("threadId") or tin.get("conversationId")
    data, _ = dp._load_json(dp._session_path(sid))
    return ((data or {}).get("codex_threads") or {}).get(tid) if tid else None


def _codex_mcp_root(inp, sid, reply=False):
    """Codex 在哪个仓库干活：续聊 → 开线程时记下的仓库（codex-reply 不带 cwd）；
    否则 tool_input.cwd 所在 git 仓库；都没有 → 按命令的规则定。"""
    if reply:
        th = _thread_of(inp, sid)
        if th and th.get("root"):
            return th["root"]
    cwd = (inp.get("tool_input") or {}).get("cwd")
    if isinstance(cwd, str) and cwd:
        base = cwd if os.path.isabs(cwd) else os.path.join(inp.get("cwd") or os.getcwd(), cwd)
        g = dp.git_root(base)
        if g:
            return g
    return _pick_root(inp, sid)


def on_post_tool(inp):
    tool = inp.get("tool_name") or ""
    mcp = _CODEX_MCP.match(tool)
    if tool not in EDIT_TOOLS + ("Bash", "PowerShell") and not mcp:
        return
    sid = inp.get("session_id")
    tin = inp.get("tool_input") or {}
    path = (tin.get("file_path") or tin.get("notebook_path")) if tool in EDIT_TOOLS else None
    root = _codex_mcp_root(inp, sid, reply=bool(mcp.group(1))) if mcp else _pick_root(inp, sid, path)
    r = dp.resolve(inp.get("cwd"), sid, bind=True, root=root)
    if mcp:
        with dp.session(sid, r["root"]) as st:
            _codex_mcp_update(st, r, inp, reply=bool(mcp.group(1)))
        return
    with dp.session(sid, r["root"]) as st:
        notes = _post_tool_update(st, r, sid, tool, inp)
    if notes:
        _out(_ctx("PostToolUse", "\n".join(notes)))


def _thread_id(resp):
    """codex 返回里的 threadId。实测 tool_response 是 JSON 字符串 {"threadId": ..., "content": ...}；也兼容已解析的 dict。"""
    if isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except ValueError:
            m = re.search(r'"threadId"\s*:\s*"([^"]+)"', resp)
            return m.group(1) if m else None
    if isinstance(resp, dict):
        tid = resp.get("threadId") or (resp.get("structuredContent") or {}).get("threadId")
        return tid if isinstance(tid, str) else None
    return None


def _codex_mcp_update(st, r, inp, reply):
    """MCP 派活记账。Codex 在 MCP 里改的文件不经 Claude 的工具：不在这里吸收进快照的话，Claude 的下一条命令
    做快照对比时会把它们算成 Claude 命令行改的（档位 3 还会记违规）。
    只读档不碰快照：它改不了文件，这期间冒出来的变化是 Claude（并行的命令、cwd 外仓库里的命令）或外部改的，
    留给下一条命令去发现（10-26 两轮审查；实测过只读调用把之前 cp 进临时克隆的文件吸收掉）。
    写模式照样可能吞掉并行命令的改动、文件数可能偏多——已知局限。行数不算：MCP 不要求工作树干净，
    相对 HEAD 的 diff 会混进之前就有的改动。"""
    tin = inp.get("tool_input") or {}
    c = st["codex"]
    threads = st.setdefault("codex_threads", {})
    if reply:
        tid = tin.get("threadId") or tin.get("conversationId")
        th = threads.get(tid) or {}
        mode = "reply"
        sandbox_mode = th.get("mode", "unknown")          # 续聊沿用开线程时的档；不认识的线程按可写处理
        c["replies"] = c.get("replies", 0) + 1
    else:
        mode = sandbox_mode = "read" if tin.get("sandbox") == "read-only" else ("write" if tin.get("sandbox") else "unknown")
        c["runs"] += 1
        c["read_runs" if mode == "read" else "write_runs"] += 1
        tid = _thread_id(inp.get("tool_response"))
        if tid:
            threads[tid] = {"root": r["root"], "mode": mode, "time": time.time()}
    changed = 0
    if sandbox_mode != "read":
        homes = [r]
        if st.get("project") and dp.norm(st["project"]) != dp.norm(r["root"]):
            homes.append(dp.resolve(inp.get("cwd"), inp.get("session_id"), root=st["project"]))
        for rr in homes:
            snaps = _snapshots(st)
            cur = dp.dirty_sources(rr)
            if cur is None:
                continue
            prev = snaps.get(dp.norm(rr["root"]))
            if isinstance(prev, dict):
                changed += sum(1 for k, m in cur.items() if prev.get(k) != m)
            snaps[dp.norm(rr["root"])] = cur
    c["files"] += changed
    dp.append_stats({"time": time.time(), "kind": "codex_run", "via": "mcp", "project": r["root"], "mode": mode,
                     "added": 0, "removed": 0, "files": changed, "session_id": inp.get("session_id")})


def _shell_changes(st, r, resp, is_dispatch, notes):
    """命令改了 r 这个仓库里哪些源码：工具结果里的列表 + 前后两次 git 快照对比。第一次见到的仓库只记基线。"""
    changed = [dp.rel_in_root(p, r["root"]) for p in _bash_changed_files(resp) if dp.is_source(p, r)]
    if r["effective"] >= 1:
        snaps = _snapshots(st)
        nroot = dp.norm(r["root"])
        cur = dp.dirty_sources(r)
        prev = snaps.get(nroot)
        if cur is not None:
            if isinstance(prev, dict) and not is_dispatch and not dp.codex_running(st):
                changed += [k for k, m in cur.items() if prev.get(k) != m]
            snaps[nroot] = cur
    for rel in dict.fromkeys(c for c in changed if c):
        key = _acct_key(st, r["root"], rel)
        if key not in st["claude"]["shell_files"]:
            st["claude"]["shell_files"].append(key)
        sd = st["since_decision"]
        if key not in sd["files"]:
            sd["files"].append(key)
        if r["effective"] >= 3 and not dp.active_waiver(st, os.path.join(r["root"], rel)):
            st["violations"].append({"time": time.time(), "kind": "shell_edit", "file": key})
            notes.append("[codex-dispatch] 档位 3：刚才的命令改了源码 %s（命令行改文件拦不住，已记为违规）。"
                         "如果这不是有意的小修，用 git 撤回并改为派给 Codex。" % rel)


def _post_tool_update(st, r, sid, tool, inp):
    tin = inp.get("tool_input") or {}
    resp = inp.get("tool_response")
    notes = []
    analysis = False

    if tool in EDIT_TOOLS:
        path = tin.get("file_path") or tin.get("notebook_path")
        if dp.is_source(path, r):
            added, removed = dp.edit_counts(tool, tin, resp)
            rel = dp.rel_in_root(path, r["root"])
            key = _acct_key(st, r["root"], rel)
            f = st["claude"]["files"].setdefault(key, {"added": 0, "removed": 0, "edits": 0})
            f["added"] += added
            f["removed"] += removed
            f["edits"] += 1
            sd = st["since_decision"]
            if key not in sd["files"]:
                sd["files"].append(key)
            sd["lines"] += added + removed
            snap = _snapshots(st).get(dp.norm(r["root"]))
            if isinstance(snap, dict):           # 编辑工具改的已记账，别再被命令行检测算一次
                try:
                    snap[dp.file_key(rel)] = os.path.getmtime(path if os.path.isabs(path) else os.path.join(r["root"], path))
                except OSError:
                    pass
        elif _is_analysis_file(path, r):
            # 同一个脚本改了又改只算一个；整份重写（Write）算新脚本；写的是编辑脚本不算，之后改它也不算
            fk = dp.norm(path if os.path.isabs(path) else os.path.join(r["root"], path))
            seen = st.setdefault("analysis_files", [])
            if tool == "Write":
                analysis = not _is_edit_script(tin.get("content"))
            else:
                analysis = fk not in seen
            if fk not in seen:
                seen.append(fk)
    else:
        cmd = tin.get("command") or ""
        if _is_analysis_command(cmd):
            analysis = True
        is_dispatch = "dispatch-codex.ps1" in cmd
        if is_dispatch and tin.get("run_in_background"):
            st["codex_running_until"] = time.time() + dp.CODEX_RUNNING_MAX
        _shell_changes(st, r, resp, is_dispatch, notes)
        # cwd 在别的仓库时，命令照样可能改到会话起始项目（cd 过去改、绝对路径改）
        home = st.get("project")
        if home and dp.norm(home) != dp.norm(r["root"]):
            _shell_changes(st, dp.resolve(inp.get("cwd"), sid, root=home), resp, is_dispatch, notes)

    eff = r["effective"]
    if analysis:
        st["analysis_scripts"] += 1
        if eff >= 1 and st["analysis_scripts"] >= st["analysis_next_remind"]:
            st["analysis_next_remind"] = st["analysis_scripts"] + dp.ANALYSIS_REMIND_EVERY
            notes.append("[codex-dispatch] 本会话已写第 %d 个分析脚本。停一下：剩下的排查/统计能不能一次写清派给 Codex 只读档？"
                         "决定自己做也要登记：%s" % (st["analysis_scripts"], _cmd('decide self --reason "..."', sid)))

    sd = st["since_decision"]
    if eff == 2 and (len(sd["files"]) >= dp.SRC_REMIND_FILES or sd["lines"] >= dp.SRC_REMIND_LINES) \
            and len(sd["files"]) >= sd["reminded_at_files"] + (dp.SRC_REMIND_FILES if sd["reminded_at_files"] else 0):
        sd["reminded_at_files"] = len(sd["files"])
        notes.append("[codex-dispatch] 自上次登记以来 Claude 已改 %d 个源码文件、%d 行（档位 2）。"
                     "接下来的实现要不要派给 Codex？必须登记决定：%s"
                     % (len(sd["files"]), sd["lines"], _cmd('decide dispatch|self --reason "..."', sid)))
    return notes


def on_stop(inp):
    sid = inp.get("session_id")
    r = dp.resolve(inp.get("cwd"), sid, root=_session_root(inp, sid))
    with dp.session(sid, r["root"]) as st:
        t = dp.session_totals(st)
        snap = [t["claude_files"], t["claude_added"], t["claude_removed"], t["shell_files"], t["codex_runs"]]
        if snap == st.get("last_stop") or not any(snap):
            return
        st["last_stop"] = snap
        line = dp.totals_line(st, r)
    _out({"systemMessage": "[codex-dispatch] " + line})


def on_session_end(inp):
    sid = inp.get("session_id")
    r = dp.resolve(inp.get("cwd"), sid, root=_session_root(inp, sid))
    st = dp.load_state(sid, r["root"])
    t = dp.session_totals(st)
    if any(t.values()) or st["decisions"] or st["violations"]:
        rec = {"time": time.time(), "kind": "session", "session_id": sid, "project": r["root"],
               "effective": r["effective"], "level": r["level"], "budget": r["budget"],
               "decisions": len(st["decisions"]), "violations": len(st["violations"])}
        rec.update(t)
        dp.append_stats(rec)
    items = [o for o in dp.load_overrides() if o.get("session_id") != sid]
    dp.save_overrides(items)
    # 会话状态留 7 天备查，过期的顺手清掉
    d = os.path.join(dp.state_dir(), "sessions")
    for fn in os.listdir(d):
        p = os.path.join(d, fn)
        try:
            if time.time() - os.path.getmtime(p) > 7 * 86400:
                os.remove(p)
        except OSError:
            pass


HANDLERS = {
    "session-start": on_session_start, "pre-tool": on_pre_tool, "post-tool": on_post_tool,
    "stop": on_stop, "session-end": on_session_end,
}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        inp = json.loads(raw) if raw.strip() else {}
        if _debug_on():
            _debug(event, inp)
        h = HANDLERS.get(event)
        if h:
            h(inp)
    except Exception:  # noqa: BLE001 —— 放行，但留下证据
        try:
            with open(os.path.join(dp.state_dir(), "errors.log"), "a", encoding="utf-8") as f:
                f.write("%s %s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), event, traceback.format_exc()))
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
