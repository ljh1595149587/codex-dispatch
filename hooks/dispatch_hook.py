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


# ---------------------------------------------------------------- 事件

def on_session_start(inp):
    sid = inp.get("session_id")
    r = dp.resolve(inp.get("cwd"), sid, bind=True)
    with dp.session(sid, r["root"]):
        pass
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
    r = dp.resolve(inp.get("cwd"), sid, bind=True)
    if r["effective"] < 3:
        return
    tin = inp.get("tool_input") or {}
    path = tin.get("file_path") or tin.get("notebook_path")
    if not dp.is_source(path, r):
        return
    with dp.session(sid, r["root"]) as st:
        if dp.active_waiver(st, path):
            return
        st["violations"].append({"time": time.time(), "kind": "blocked_edit", "file": path})
    reason = ("[codex-dispatch] 生效档位 3：项目源码由 Codex 实现，Claude 不直接改。"
              "请把这项改动写成需求派给 Codex（codex-dispatch skill，写模式）。"
              "确属必须自己做的小修，先登记理由再改：" + _cmd('decide self --reason "..." --files ' + (dp.rel_in_root(path, r["root"]) or os.path.basename(path)), sid))
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


def _is_analysis_command(cmd):
    if not cmd or not _INLINE.search(cmd):
        return False
    return len(cmd) >= ANALYSIS_MIN_CHARS or cmd.count("\n") + 1 >= ANALYSIS_MIN_LINES


def _is_analysis_file(path, r):
    if not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".py", ".sh", ".ps1"):
        return False
    rel = dp.rel_in_root(path, r["root"])
    # 项目外（scratchpad、临时目录）写的脚本 = 一次性分析脚本；项目内写脚本算源码改动
    return rel is None or re.search(r"(^|/)(scratch|scratchpad|tmp|temp)/", rel, re.I) is not None


def on_post_tool(inp):
    tool = inp.get("tool_name")
    if tool not in EDIT_TOOLS + ("Bash",):
        return
    sid = inp.get("session_id")
    r = dp.resolve(inp.get("cwd"), sid, bind=True)
    with dp.session(sid, r["root"]) as st:
        notes = _post_tool_update(st, r, sid, tool, inp)
    if notes:
        _out(_ctx("PostToolUse", "\n".join(notes)))


def _post_tool_update(st, r, sid, tool, inp):
    tin = inp.get("tool_input") or {}
    resp = inp.get("tool_response")
    notes = []
    analysis = False

    if tool in EDIT_TOOLS:
        path = tin.get("file_path") or tin.get("notebook_path")
        if dp.is_source(path, r):
            added, removed = dp.edit_counts(tool, tin, resp)
            key = dp.file_key(dp.rel_in_root(path, r["root"]))
            f = st["claude"]["files"].setdefault(key, {"added": 0, "removed": 0, "edits": 0})
            f["added"] += added
            f["removed"] += removed
            f["edits"] += 1
            sd = st["since_decision"]
            if key not in sd["files"]:
                sd["files"].append(key)
            sd["lines"] += added + removed
        elif _is_analysis_file(path, r):
            analysis = True
    else:
        if _is_analysis_command(tin.get("command")):
            analysis = True
        for p in _bash_changed_files(resp):
            if dp.is_source(p, r):
                key = dp.file_key(dp.rel_in_root(p, r["root"]))
                if key not in st["claude"]["shell_files"]:
                    st["claude"]["shell_files"].append(key)
                sd = st["since_decision"]
                if key not in sd["files"]:
                    sd["files"].append(key)
                if r["effective"] >= 3 and not dp.active_waiver(st, os.path.join(r["root"], key)):
                    st["violations"].append({"time": time.time(), "kind": "shell_edit", "file": key})
                    notes.append("[codex-dispatch] 档位 3：刚才的命令改了源码 %s（命令行改文件拦不住，已记为违规）。"
                                 "如果这不是有意的小修，用 git 撤回并改为派给 Codex。" % key)

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
    r = dp.resolve(inp.get("cwd"), sid)
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
    r = dp.resolve(inp.get("cwd"), sid)
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
