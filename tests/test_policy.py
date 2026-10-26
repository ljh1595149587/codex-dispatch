# -*- coding: utf-8 -*-
"""dispatch_policy.py 与 hooks/dispatch_hook.py 的离线单测：不调 Codex、不联网、不碰真实 ~/.claude。

运行：python -m unittest discover -s tests -p "test_*.py"
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HOOK = os.path.join(REPO, "hooks", "dispatch_hook.py")
POLICY = os.path.join(REPO, "bin", "dispatch_policy.py")
sys.path.insert(0, os.path.join(REPO, "bin"))
import dispatch_policy as dp  # noqa: E402


class Env(unittest.TestCase):
    """每个用例一个隔离的「用户目录」和一个 git 项目。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cdp-")
        self.home = os.path.join(self.tmp, "home")
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.home)
        os.makedirs(self.proj)
        subprocess.run(["git", "init", "-q", self.proj], check=True)
        self._old = os.environ.get("CODEX_DISPATCH_HOME")
        os.environ["CODEX_DISPATCH_HOME"] = self.home
        os.environ.pop("CODEX_DISPATCH_HOOK_DEBUG", None)
        # 在 Claude Code 里跑测试时会继承真实会话 ID，CLI 会改记到那个会话上
        self._old_sid = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def tearDown(self):
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        if self._old_sid is not None:
            os.environ["CLAUDE_CODE_SESSION_ID"] = self._old_sid
        if self._old is None:
            os.environ.pop("CODEX_DISPATCH_HOME", None)
        else:
            os.environ["CODEX_DISPATCH_HOME"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def user_cfg(self, **kw):
        with open(os.path.join(self.home, "codex-dispatch.json"), "w", encoding="utf-8") as f:
            json.dump(kw, f)

    def proj_cfg(self, data):
        os.makedirs(os.path.join(self.proj, ".claude"), exist_ok=True)
        with open(os.path.join(self.proj, ".claude", "codex-dispatch.json"), "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def hook(self, event, payload, proc_cwd=None):
        payload.setdefault("session_id", "s1")
        payload.setdefault("cwd", self.proj)
        p = subprocess.run([sys.executable, HOOK, event], input=json.dumps(payload).encode("utf-8"),
                           capture_output=True, env=os.environ.copy(), cwd=proc_cwd)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8").strip()
        return json.loads(out) if out else None

    def policy(self, *args):
        p = subprocess.run([sys.executable, POLICY, "--cwd", self.proj] + list(args),
                           capture_output=True, env=os.environ.copy())
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def edit(self, rel, old="a\n", new="a\nb\n", tool="Edit"):
        tin = {"file_path": os.path.join(self.proj, rel), "old_string": old, "new_string": new}
        if tool == "Write":
            tin = {"file_path": os.path.join(self.proj, rel), "content": new}
        return self.hook("post-tool", {"tool_name": tool, "tool_input": tin, "tool_response": {}})

    def errors(self):
        p = os.path.join(self.home, "codex-dispatch-state", "errors.log")
        if not os.path.exists(p):
            return ""
        with open(p, encoding="utf-8") as f:
            return f.read()


class TestLevels(Env):
    def test_effective_table(self):
        exp = {("ample", 0): 0, ("ample", 1): 1, ("ample", 2): 2, ("ample", 3): 3,
               ("tight", 0): 2, ("tight", 1): 2, ("tight", 2): 3, ("tight", 3): 3}
        for (b, lv), want in exp.items():
            self.assertEqual(dp.effective_level(lv, b), want, (b, lv))

    def test_defaults_without_any_config(self):
        r = dp.resolve(self.proj)
        self.assertEqual((r["level"], r["budget"], r["effective"], r["warnings"]), (1, "ample", 1, []))

    def test_project_level_and_tight_budget(self):
        self.user_cfg(claude_budget="tight", default_level=0)
        self.proj_cfg({"level": 2})
        r = dp.resolve(self.proj)
        self.assertEqual((r["level"], r["level_source"], r["effective"]), (2, "项目", 3))

    def test_invalid_configs_warn_and_fall_back(self):
        self.user_cfg(claude_budget="huge", default_level=7)
        self.proj_cfg("{not json")
        r = dp.resolve(self.proj)
        self.assertEqual((r["level"], r["budget"]), (1, "ample"))
        self.assertEqual(len(r["warnings"]), 3, r["warnings"])

    def test_bool_is_not_a_level(self):
        self.proj_cfg({"level": True})
        r = dp.resolve(self.proj)
        self.assertEqual(r["level"], 1)
        self.assertTrue(r["warnings"])

    def test_override_applies_and_clears(self):
        self.proj_cfg({"level": 1})
        self.assertEqual(self.policy("override", "--level", "3")[0], 0)
        self.hook("session-start", {})
        self.assertEqual(dp.resolve(self.proj, "s1")["effective"], 3)
        # 另一个会话看不到已被 s1 认领的覆盖
        self.assertEqual(dp.resolve(self.proj, "s2")["effective"], 1)
        self.hook("session-end", {})
        self.assertEqual(dp.resolve(self.proj, "s1")["effective"], 1)


class TestSourceAndCounts(Env):
    def test_is_source(self):
        r = dp.resolve(self.proj)
        j = lambda *p: os.path.join(self.proj, *p)  # noqa: E731
        self.assertTrue(dp.is_source(j("src", "a.go"), r))
        self.assertTrue(dp.is_source("src/a.py", r))
        self.assertFalse(dp.is_source(j("README.md"), r))
        self.assertFalse(dp.is_source(j("docs", "x.py"), r))
        self.assertFalse(dp.is_source(j("node_modules", "x", "y.js"), r))
        self.assertFalse(dp.is_source(os.path.join(self.tmp, "elsewhere.go"), r))
        self.assertFalse(dp.is_source(j("docs", "dispatch-log.md"), r))

    def test_custom_exts(self):
        self.proj_cfg({"level": 1, "source_exts": [".md"], "exclude": []})
        r = dp.resolve(self.proj)
        self.assertTrue(dp.is_source(os.path.join(self.proj, "docs", "a.md"), r))

    def test_diff_counts_ignore_context(self):
        self.assertEqual(dp.diff_counts("a\nb\nc\n", "a\nX\nc\nd\n"), (2, 1))
        self.assertEqual(dp.diff_counts("", "x\ny\n"), (2, 0))

    def test_structured_patch_preferred(self):
        resp = {"structuredPatch": [{"lines": [" a", "-b", "+c", "+d"]}]}
        self.assertEqual(dp.edit_counts("Edit", {"old_string": "zzz", "new_string": "q"}, resp), (2, 1))


class TestHooks(Env):
    def test_session_start_reports_level(self):
        self.proj_cfg({"level": 2})
        out = self.hook("session-start", {})
        self.assertIn("生效 2", out["systemMessage"])
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("执行者", ctx)
        self.assertIn("decide", ctx)

    def test_level3_blocks_source_edit_but_not_docs(self):
        self.proj_cfg({"level": 3})
        deny = self.hook("pre-tool", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.proj, "a.go")}})
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.hook("pre-tool", {"tool_name": "Edit",
                                                 "tool_input": {"file_path": os.path.join(self.proj, "notes.md")}}))

    def test_level3_waiver_allows_listed_file_only(self):
        self.proj_cfg({"level": 3})
        self.hook("session-start", {})
        self.assertEqual(self.policy("decide", "self", "--reason", "改一行常量", "--files", "a.go")[0], 0)
        a = self.hook("pre-tool", {"tool_name": "Write", "tool_input": {"file_path": os.path.join(self.proj, "a.go")}})
        b = self.hook("pre-tool", {"tool_name": "Write", "tool_input": {"file_path": os.path.join(self.proj, "b.go")}})
        self.assertIsNone(a)
        self.assertEqual(b["hookSpecificOutput"]["permissionDecision"], "deny")
        with open(os.path.join(self.proj, "docs", "dispatch-log.md"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn("改一行常量", log)

    def test_level2_reminds_after_three_files_then_every_three(self):
        self.proj_cfg({"level": 2})
        self.assertIsNone(self.edit("a.go"))
        self.assertIsNone(self.edit("b.go"))
        self.assertIn("必须登记", self.edit("c.go")["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.edit("c.go"))   # 同一文件再改不重复提醒
        self.assertIsNone(self.edit("d.go"))
        self.assertIsNone(self.edit("e.go"))
        self.assertIsNotNone(self.edit("f.go"))
        self.assertEqual(self.policy("decide", "dispatch", "--reason", "剩下的派出去")[0], 0)
        self.assertIsNone(self.edit("g.go"))   # 登记后计数归零

    def test_level2_line_threshold(self):
        self.proj_cfg({"level": 2})
        out = self.edit("big.go", old="", new="x\n" * 90, tool="Write")
        self.assertIsNotNone(out)

    def test_level1_no_source_reminder(self):
        self.proj_cfg({"level": 1})
        for n in "abcdef":
            self.assertIsNone(self.edit(n + ".go"))

    def test_analysis_script_reminder(self):
        self.proj_cfg({"level": 1})
        cmd = "python - <<'EOF'\n" + "print(1)\n" * 10 + "EOF"
        bash = lambda c: self.hook("post-tool", {"tool_name": "Bash", "tool_input": {"command": c}, "tool_response": {}})  # noqa: E731
        self.assertIsNone(bash("ls -la"))
        self.assertIsNone(bash(cmd))
        self.assertIn("第 2 个分析脚本", bash(cmd)["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(bash(cmd))
        self.assertIsNone(bash(cmd))
        self.assertIsNotNone(bash(cmd))

    def test_analysis_file_outside_project(self):
        self.proj_cfg({"level": 1})
        w = lambda: self.hook("post-tool", {"tool_name": "Write", "tool_input": {  # noqa: E731
            "file_path": os.path.join(self.tmp, "scratch", "x.py"), "content": "print(1)\n"}, "tool_response": {}})
        self.assertIsNone(w())
        self.assertIsNotNone(w())

    def test_edit_scripts_are_not_analysis(self):
        # 两次真实误报的形状：读文件 → replace → 写回；合并 JSON 配置
        self.proj_cfg({"level": 1})
        replace_back = ("cd proj && python - <<'EOF'\np='task_plan.md'; s=open(p,encoding='utf-8').read()\n"
                        + "s=s.replace('旧的一段很长的文字……','新的一段很长的文字……')\n" * 6
                        + "open(p,'w',encoding='utf-8').write(s)\nEOF")
        merge_json = ("python - <<'EOF'\nimport json,os\np=os.path.expanduser('~/.claude/settings.json')\n"
                      "d=json.load(open(p,encoding='utf-8'))\nd.setdefault('hooks',{})\n"
                      "d['hooks']['Stop']=[{'hooks':[{'type':'command','command':'python x.py stop'}]}]\n"
                      "with open(p+'.tmp','w',encoding='utf-8') as f:\n    json.dump(d,f,ensure_ascii=False,indent=2)\n"
                      "os.replace(p+'.tmp',p)\nprint('ok')\nEOF")
        for c in (replace_back, merge_json) * 2:   # 算分析的话第 2 次就会提醒
            self.assertIsNone(self.hook("post-tool", {"tool_name": "Bash", "tool_input": {"command": c}, "tool_response": {}}))

    def test_stats_that_write_a_report_still_count(self):
        self.proj_cfg({"level": 1})
        cmd = ("python - <<'EOF'\nimport json\nfrom collections import Counter\nc=Counter()\n"
               "for l in open('app.log',encoding='utf-8'):\n    c[l.split()[2]]+=1\n"
               "json.dump(c.most_common(20),open('/tmp/top.json','w'))\nprint(c.most_common(5))\nEOF")
        bash = lambda: self.hook("post-tool", {"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_response": {}})  # noqa: E731
        self.assertIsNone(bash())
        self.assertIsNotNone(bash())

    def test_json_dump_to_stdout_is_not_a_write(self):
        self.proj_cfg({"level": 1})
        cmd = "python - <<'EOF'\nimport json,sys\nd=json.load(open('a.json'))\n" + "x=1\n" * 6 + "json.dump(d,sys.stdout)\nEOF"
        bash = lambda: self.hook("post-tool", {"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_response": {}})  # noqa: E731
        self.assertIsNone(bash())
        self.assertIsNotNone(bash())

    def test_scratch_edit_script_file_not_counted(self):
        self.proj_cfg({"level": 1})
        body = "p='task_plan.md'\ns=open(p,encoding='utf-8').read().replace('a','b')\nopen(p,'w',encoding='utf-8').write(s)\n"
        w = lambda: self.hook("post-tool", {"tool_name": "Write", "tool_input": {  # noqa: E731
            "file_path": os.path.join(self.tmp, "scratch", "fix.py"), "content": body}, "tool_response": {}})
        for _ in range(3):
            self.assertIsNone(w())

    def test_edit_outside_project_not_in_temp_not_counted(self):
        # cwd 漂到别的目录后改本仓库的 .py：项目外、不在临时目录 ⇒ 不是分析脚本
        self.proj_cfg({"level": 1})
        other = os.path.join(os.path.splitdrive(self.tmp)[0] + os.sep, "work", "other-repo", "hook.py")
        e = lambda: self.hook("post-tool", {"tool_name": "Edit", "tool_input": {  # noqa: E731
            "file_path": other, "old_string": "a", "new_string": "b"}, "tool_response": {}})
        for _ in range(3):
            self.assertIsNone(e())

    def test_write_and_stdout_forms(self):
        # 两轮审查 10-26 提出的写法（本次修的那几条）
        import importlib.util
        spec = importlib.util.spec_from_file_location("dispatch_hook", HOOK)
        h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(h)
        for code in ("open(p,'wt',encoding='utf-8').write(s)", "open(Path(p), 'w').write(s)",
                     "open(mode='w', file=p).write(s)", "io.open(p,'w',encoding='utf-8',newline='').write(s)",
                     "open(p, \"a\").write(s)", "open(p,'r+').write(s)", "json.dump(d, open(p, 'w'))"):
            self.assertTrue(h._is_edit_script(code), code)
        for code in ("s=open(p).read()", "s=open(p,'r',encoding='utf-8').read()", "s=open(p,'rb').read()",
                     "json.dump(sorted(rows), sys.stdout)", "open(p).read().replace(',', 'w')"):
            self.assertFalse(h._is_edit_script(code), code)
        self.assertFalse(h._is_edit_script(123))

    def test_write_non_string_content_still_counts(self):
        self.proj_cfg({"level": 1})
        w = lambda: self.hook("post-tool", {"tool_name": "Write", "tool_input": {  # noqa: E731
            "file_path": os.path.join(self.tmp, "scratch", "x.py"), "content": 123}, "tool_response": {}})
        self.assertIsNone(w())
        self.assertIsNotNone(w())          # 没抛异常、照常计数
        self.assertEqual(self.errors(), "")

    def test_relative_path_outside_root_resolved_against_root(self):
        # proj 在 <临时目录>/cdp-*/proj：../x.py 按项目根解析落在临时目录 ⇒ 算分析脚本；
        # 钩子进程 cwd 放在盘符根，按进程 cwd 解析会得到 <盘符>/x.py ⇒ 不算（旧代码的错）
        self.proj_cfg({"level": 1})
        drive_root = os.path.splitdrive(self.tmp)[0] + os.sep
        w = lambda: self.hook("post-tool", {"tool_name": "Write", "tool_input": {  # noqa: E731
            "file_path": os.path.join("..", "x.py"), "content": "print(1)\n"}, "tool_response": {}}, proc_cwd=drive_root)
        self.assertIsNone(w())
        self.assertIsNotNone(w())

    def test_repeated_edit_of_same_scratch_script_counts_once(self):
        self.proj_cfg({"level": 1})
        p = os.path.join(self.tmp, "scratch", "probe.py")
        e = lambda: self.hook("post-tool", {"tool_name": "Edit", "tool_input": {  # noqa: E731
            "file_path": p, "old_string": "a", "new_string": "b"}, "tool_response": {}})
        for _ in range(4):
            self.assertIsNone(e())

    def test_long_path_does_not_inflate_command_length(self):
        self.proj_cfg({"level": 1})
        long_path = os.path.join(self.tmp, "scratchpad", "x" * 60, "y" * 60, "data.json").replace("\\", "/")
        cmd = "python -c \"import json;print(len(json.load(open('%s'))))\" && awk '{n++} END{print n}' %s" % (long_path, long_path)
        self.assertGreaterEqual(len(cmd), 300)
        for _ in range(3):
            self.assertIsNone(self.hook("post-tool", {"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_response": {}}))

    def test_level0_never_reminds(self):
        self.proj_cfg({"level": 0})
        cmd = "python - <<'EOF'\n" + "print(1)\n" * 10 + "EOF"
        for _ in range(4):
            self.assertIsNone(self.hook("post-tool", {"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_response": {}}))

    def test_bash_changed_files_counted_and_flagged_at_level3(self):
        self.proj_cfg({"level": 3})
        out = self.hook("post-tool", {"tool_name": "Bash", "tool_input": {"command": "sed -i s/a/b/ a.go"},
                                      "tool_response": {"changedFiles": [os.path.join(self.proj, "a.go"), "README.md"]}})
        self.assertIn("违规", out["hookSpecificOutput"]["additionalContext"])
        st = dp.load_state("s1", self.proj)
        self.assertEqual(st["claude"]["shell_files"], ["a.go"])

    def test_stop_only_when_changed(self):
        self.proj_cfg({"level": 1})
        self.assertIsNone(self.hook("stop", {}))
        self.edit("a.go")
        msg = self.hook("stop", {})["systemMessage"]
        self.assertIn("Claude 改 1 文件 +1/-0", msg)
        self.assertIsNone(self.hook("stop", {}))

    def test_session_end_writes_stats(self):
        self.edit("a.go")
        self.hook("session-end", {})
        with open(os.path.join(self.home, "codex-dispatch-state", "stats.jsonl"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(json.loads(lines[-1])["claude_files"], 1)

    def test_garbage_input_is_swallowed(self):
        p = subprocess.run([sys.executable, HOOK, "post-tool"], input=b"{not json", capture_output=True, env=os.environ.copy())
        self.assertEqual((p.returncode, p.stdout), (0, b""))
        self.assertIn("JSONDecodeError", self.errors())


class TestGit(Env):
    def _commit_base(self):
        with open(os.path.join(self.proj, "a.go"), "w") as f:
            f.write("one\n")
        g = ["git", "-C", self.proj, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-qm", "base"], check=True)

    def test_record_codex_from_git_excludes_log(self):
        self._commit_base()
        with open(os.path.join(self.proj, "a.go"), "a") as f:
            f.write("two\nthree\n")
        os.makedirs(os.path.join(self.proj, "tests"))
        with open(os.path.join(self.proj, "tests", "t.go"), "w") as f:
            f.write("x\n")
        self.policy("decide", "dispatch", "--reason", "r")   # 写一条派活日志，不应计入
        added, removed, n, files = dp.git_changes(self.proj)
        self.assertEqual((added, removed, n), (3, 0, 2), files)
        code, out = self.policy("record-codex-from-git", "--root", self.proj)
        self.assertEqual(code, 0)
        self.assertIn("+3/-0", out)

    def test_check_protected(self):
        self._commit_base()
        os.makedirs(os.path.join(self.proj, "tests", "acceptance"))
        with open(os.path.join(self.proj, "tests", "acceptance", "x_test.go"), "w") as f:
            f.write("x\n")
        code, out = self.policy("check-protected", "--root", self.proj, "--globs", "tests/acceptance/*")
        self.assertEqual(code, 1)
        self.assertIn("tests/acceptance/x_test.go", out)
        self.assertEqual(self.policy("check-protected", "--root", self.proj, "--globs", "other/*")[0], 0)


class TestReviewFindings(Env):
    """2026-10-26 Codex 两轮审查 8 条成立发现的回归测试（编号同审查表顺序）。"""

    def test_1_parallel_hooks_do_not_lose_updates(self):
        self.proj_cfg({"level": 1})
        procs = []
        for i in range(8):
            payload = {"session_id": "s1", "cwd": self.proj, "tool_name": "Edit", "tool_response": {},
                       "tool_input": {"file_path": os.path.join(self.proj, "f%d.go" % i), "old_string": "", "new_string": "x\n"}}
            pr = subprocess.Popen([sys.executable, HOOK, "post-tool"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
            procs.append((pr, json.dumps(payload).encode("utf-8")))
        for pr, data in procs:
            pr.stdin.write(data)
            pr.stdin.close()
        for pr, _ in procs:
            pr.wait()
            pr.stdout.close()
            pr.stderr.close()
        self.assertEqual(len(dp.load_state("s1", self.proj)["claude"]["files"]), 8)

    def test_2_override_can_be_changed_in_same_session(self):
        self.policy("--session", "s1", "override", "--level", "1")
        self.policy("--session", "s1", "override", "--level", "3")
        self.assertEqual(dp.resolve(self.proj, "s1")["level"], 3)
        self.policy("--session", "s1", "override", "--clear")
        self.assertEqual(dp.resolve(self.proj, "s1")["level_source"], "用户默认")

    def test_3_broken_project_config_falls_back_to_user_default(self):
        # 设计：项目配置坏了回落到「用户默认档位」（不是写死的 1），并带警告
        self.user_cfg(default_level=3)
        self.proj_cfg("{broken")
        r = dp.resolve(self.proj)
        self.assertEqual(r["level"], 3)
        self.assertTrue(r["warnings"])

    def test_4_decide_goes_to_named_session(self):
        self.proj_cfg({"level": 3})
        self.hook("session-start", {"session_id": "s1"})
        self.hook("session-start", {"session_id": "s2"})   # s2 更晚更新
        self.policy("--session", "s1", "decide", "self", "--reason", "r", "--files", "a.go")
        self.assertTrue(dp.load_state("s1", self.proj)["waivers"])
        self.assertFalse(dp.load_state("s2", self.proj)["waivers"])

    def test_5_waiver_with_dir_is_exact(self):
        self.proj_cfg({"level": 3})
        self.hook("session-start", {})
        self.policy("--session", "s1", "decide", "self", "--reason", "r", "--files", "src/a.py")
        pre = lambda rel: self.hook("pre-tool", {"tool_name": "Edit",  # noqa: E731
                                                  "tool_input": {"file_path": os.path.join(self.proj, *rel.split("/"))}})
        self.assertIsNone(pre("src/a.py"))
        self.assertIsNotNone(pre("other/src/a.py"))
        self.policy("--session", "s1", "decide", "self", "--reason", "r", "--files", "b.py")
        self.assertIsNone(pre("deep/x/b.py"))   # 只写文件名：按文件名放行

    @unittest.skipUnless(os.name == "nt", "大小写不敏感只在 Windows 上成立")
    def test_6_case_variants_are_one_file(self):
        self.proj_cfg({"level": 2})
        self.assertIsNone(self.edit("src/a.py"))
        self.assertIsNone(self.edit("SRC/a.py"))
        self.assertIsNone(self.edit("src/A.py"))
        self.assertEqual(len(dp.load_state("s1", self.proj)["claude"]["files"]), 1)

    def test_7_untracked_docs_dir_lists_log_file(self):
        # dispatch-codex.ps1 用 --untracked-files=all：否则新建的 docs/ 只显示成「?? docs/」，排除规则匹配不上
        self.policy("decide", "dispatch", "--reason", "r")
        out = subprocess.run(["git", "-C", self.proj, "status", "--porcelain", "--untracked-files=all"],
                             capture_output=True).stdout.decode()
        self.assertIn("docs/dispatch-log.md", out)

    @unittest.skipUnless(os.name == "nt", "大小写不敏感只在 Windows 上成立")
    def test_8_log_exclusion_ignores_case(self):
        os.makedirs(os.path.join(self.proj, "Docs"))
        with open(os.path.join(self.proj, "Docs", "Dispatch-Log.md"), "w") as f:
            f.write("x\n")
        self.assertEqual(dp.git_changes(self.proj)[2], 0)

    def test_9_non_ascii_filenames_counted(self):
        g = ["git", "-C", self.proj, "-c", "user.name=t", "-c", "user.email=t@t"]
        with open(os.path.join(self.proj, "base.txt"), "w") as f:
            f.write("b\n")
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-qm", "base"], check=True)
        with open(os.path.join(self.proj, "中文.go"), "w", encoding="utf-8") as f:
            f.write("a\nb\nc\n")
        added, _, n, files = dp.git_changes(self.proj)
        self.assertEqual((added, n), (3, 1), files)


class TestShellDetection(Env):
    """命令行改文件：Claude Code 给 Bash 钩子的结果里没有改动文件列表（2026-10-26 实测），靠 git 快照前后对比。"""

    def put(self, rel, text="x\n"):
        full = os.path.join(self.proj, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
        return full

    def bash(self, cmd, **extra):
        tin = {"command": cmd}
        tin.update(extra)
        return self.hook("post-tool", {"tool_name": "Bash", "tool_input": tin, "tool_response": {"stdout": ""}})

    def shell_files(self):
        return dp.load_state("s1", self.proj)["claude"]["shell_files"]

    def test_shell_edit_detected(self):
        self.proj_cfg({"level": 1})
        self.put("pre.go")                       # 会话开始前就有的改动不算
        self.hook("session-start", {})
        self.put("a.go")
        self.bash("sed -i s/x/y/ a.go")
        self.assertEqual(self.shell_files(), ["a.go"])

    def test_edit_tool_change_not_double_counted(self):
        self.proj_cfg({"level": 1})
        self.hook("session-start", {})
        full = self.put("b.go")
        self.hook("post-tool", {"tool_name": "Write", "tool_response": {},
                                "tool_input": {"file_path": full, "content": "x\n"}})
        self.bash("ls")
        self.assertEqual(self.shell_files(), [])

    def test_dispatch_command_not_attributed(self):
        self.proj_cfg({"level": 1})
        self.hook("session-start", {})
        self.put("c.go")
        self.bash("powershell -File dispatch-codex.ps1 -Mode write -Prompt x")
        self.bash("ls")
        self.assertEqual(self.shell_files(), [])

    def test_background_dispatch_suppresses_until_recorded(self):
        self.proj_cfg({"level": 1})
        self.hook("session-start", {})
        self.bash("powershell -File dispatch-codex.ps1 -Mode write -Prompt x", run_in_background=True)
        self.put("by_codex.go")                  # Codex 在后台改的
        self.bash("ls")
        self.assertEqual(self.shell_files(), [])
        self.policy("--session", "s1", "record-codex", "--mode", "read")   # 派活结束：清标记、刷新快照
        self.put("by_claude.go")
        self.bash("ls")
        self.assertEqual(self.shell_files(), ["by_claude.go"])

    def test_powershell_tool_counts_too(self):
        self.proj_cfg({"level": 1})
        self.hook("session-start", {})
        self.put("p.go")
        self.hook("post-tool", {"tool_name": "PowerShell", "tool_input": {"command": "Set-Content p.go y"},
                                "tool_response": {}})
        self.assertEqual(self.shell_files(), ["p.go"])

    def test_level3_shell_edit_flagged(self):
        self.proj_cfg({"level": 3})
        self.hook("session-start", {})
        self.put("v.go")
        out = self.bash("echo y > v.go")
        self.assertIn("违规", out["hookSpecificOutput"]["additionalContext"])


class TestCwdDrift(Env):
    """Bash 里 cd 走之后，钩子输入的 cwd 也跟着走（10-26 实测）。"""

    def setUp(self):
        super().setUp()
        self.plain = os.path.join(self.tmp, "plain")          # 不是 git 仓库
        self.other = os.path.join(self.tmp, "other")          # 另一个 git 仓库
        os.makedirs(self.plain)
        os.makedirs(self.other)
        subprocess.run(["git", "init", "-q", self.other], check=True)

    def test_edits_count_for_session_project_when_cwd_in_plain_dir(self):
        self.proj_cfg({"level": 2})
        self.hook("session-start", {})
        ed = lambda n: self.hook("post-tool", {"cwd": self.plain, "tool_name": "Edit", "tool_input": {  # noqa: E731
            "file_path": os.path.join(self.proj, n), "old_string": "a\n", "new_string": "a\nb\n"}, "tool_response": {}})
        self.assertIsNone(ed("a.go"))
        self.assertIsNone(ed("b.go"))
        self.assertIn("必须登记", ed("c.go")["hookSpecificOutput"]["additionalContext"])

    def test_edit_in_project_py_from_plain_cwd_is_not_analysis(self):
        self.proj_cfg({"level": 1})
        self.hook("session-start", {})
        ed = lambda: self.hook("post-tool", {"cwd": self.plain, "tool_name": "Edit", "tool_input": {  # noqa: E731
            "file_path": os.path.join(self.proj, "hooks", "h.py"), "old_string": "a", "new_string": "b"}, "tool_response": {}})
        for _ in range(3):
            self.assertIsNone(ed())

    def test_other_repo_level_applies_to_its_files(self):
        self.proj_cfg({"level": 1})
        os.makedirs(os.path.join(self.other, ".claude"))
        with open(os.path.join(self.other, ".claude", "codex-dispatch.json"), "w", encoding="utf-8") as f:
            json.dump({"level": 3}, f)
        self.hook("session-start", {})
        deny = self.hook("pre-tool", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.other, "x.go")}})
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.hook("pre-tool", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.proj, "x.go")}}))

    def test_switching_repos_does_not_blame_preexisting_dirty_files(self):
        self.proj_cfg({"level": 3})
        with open(os.path.join(self.proj, "old.go"), "w") as f:
            f.write("package x\n")                              # 会话开始前就有的改动
        with open(os.path.join(self.other, "o.go"), "w") as f:
            f.write("package o\n")
        self.hook("session-start", {})
        bash = lambda cwd: self.hook("post-tool", {"cwd": cwd, "tool_name": "Bash",  # noqa: E731
                                                   "tool_input": {"command": "ls"}, "tool_response": {}})
        self.assertIsNone(bash(self.other))
        self.assertIsNone(bash(self.proj))
        self.assertIsNone(bash(self.other))

    def test_shell_edit_of_session_project_seen_from_plain_cwd(self):
        self.proj_cfg({"level": 3})
        self.hook("session-start", {})
        with open(os.path.join(self.proj, "new.go"), "w") as f:
            f.write("package x\n")
        out = self.hook("post-tool", {"cwd": self.plain, "tool_name": "Bash",
                                      "tool_input": {"command": "cd ../proj && echo > new.go"}, "tool_response": {}})
        self.assertIn("违规", out["hookSpecificOutput"]["additionalContext"])

    def test_stop_reports_session_project(self):
        self.proj_cfg({"level": 1})
        self.hook("session-start", {})
        self.hook("post-tool", {"cwd": self.plain, "tool_name": "Write", "tool_input": {
            "file_path": os.path.join(self.proj, "a.go"), "content": "x\n"}, "tool_response": {}})
        out = self.hook("stop", {"cwd": self.plain})
        self.assertIn("Claude 改 1 文件", out["systemMessage"])

    def test_foreign_repo_keys_do_not_collide_with_project_paths(self):
        # 起始项目里的 other/x.go 与仓库 other 里的 x.go 是两个文件
        self.proj_cfg({"level": 1})
        self.hook("session-start", {})
        for p in (os.path.join(self.proj, "other", "x.go"), os.path.join(self.other, "x.go")):
            self.hook("post-tool", {"tool_name": "Write", "tool_input": {"file_path": p, "content": "x\n"}, "tool_response": {}})
        self.assertIn("Claude 改 2 文件", self.hook("stop", {})["systemMessage"])

    def test_waiver_from_deny_message_works_for_foreign_repo_only(self):
        import re
        self.proj_cfg({"level": 3})
        os.makedirs(os.path.join(self.other, ".claude"))
        with open(os.path.join(self.other, ".claude", "codex-dispatch.json"), "w", encoding="utf-8") as f:
            json.dump({"level": 3}, f)
        self.hook("session-start", {})
        pre = lambda p: self.hook("pre-tool", {"tool_name": "Edit", "tool_input": {"file_path": p}})  # noqa: E731
        foreign, mine = os.path.join(self.other, "src", "x.go"), os.path.join(self.proj, "src", "x.go")
        reason = pre(foreign)["hookSpecificOutput"]["permissionDecisionReason"]
        files = re.search(r'--files "([^"]+)"', reason).group(1)        # 照提示登记
        self.assertEqual(self.policy("--session", "s1", "decide", "self", "--reason", "小修", "--files", files)[0], 0)
        self.assertIsNone(pre(foreign))
        self.assertEqual(pre(mine)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_compact_awk_stats_program_still_counts(self):
        self.proj_cfg({"level": 1})
        prog = "{sx+=$1;sy+=$2;sxx+=$1*$1;syy+=$2*$2;sxy+=$1*$2;n++}END{mx=sx/n;my=sy/n;vx=sxx/n-mx*mx;vy=syy/n-my*my;" \
               "cv=sxy/n-mx*my;b=cv/vx;a=my-b*mx;r=cv/sqrt(vx*vy);printf(\"%.4f %.4f %.4f %.4f %.4f %.4f\\n\",mx,my,vx,vy,b,r);" \
               "print a,b,r,n,mx,my,vx,vy,cv,sx/n,sy/n,sxx/n,syy/n,sxy/n;print (sx-sy)/n,(sxx-syy)/n,sqrt(vx),sqrt(vy)}"
        cmd = "awk '%s' data.txt" % prog
        self.assertGreaterEqual(len(cmd), 300)
        bash = lambda: self.hook("post-tool", {"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_response": {}})  # noqa: E731
        self.assertIsNone(bash())
        self.assertIsNotNone(bash())

    def _record_codex(self, root, sid=None):
        env = os.environ.copy()
        if sid:
            env["CLAUDE_CODE_SESSION_ID"] = sid
        p = subprocess.run([sys.executable, POLICY, "--cwd", root, "record-codex", "--mode", "read"],
                           capture_output=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))

    def test_codex_run_into_new_project_counts_for_dispatching_session(self):
        # 在 proj 的会话里新建项目 other 并往它派活；other 里随后开了会话 s2，也不能把这次认走
        self.proj_cfg({"level": 1})
        self.hook("session-start", {})
        self.hook("session-start", {"session_id": "s2", "cwd": self.other})
        self._record_codex(self.other, sid="s1")
        self.assertIn("Codex 派活 1 次", self.hook("stop", {})["systemMessage"])
        self.assertIsNone(self.hook("stop", {"session_id": "s2", "cwd": self.other}))   # s2 什么都没做

    def _cli(self, root, sid, *args):
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)
        p = subprocess.run([sys.executable, POLICY, "--cwd", root] + list(args), capture_output=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))

    def test_cross_project_relative_waiver_applies_to_that_project(self):
        # 会话起始于 proj，在 other 里 decide self --files src/x.go：放行 other/src/x.go，不放行 proj/src/x.go
        for root in (self.proj, self.other):
            os.makedirs(os.path.join(root, ".claude"), exist_ok=True)
            with open(os.path.join(root, ".claude", "codex-dispatch.json"), "w", encoding="utf-8") as f:
                json.dump({"level": 3}, f)
        self.hook("session-start", {})
        self._cli(self.other, "s1", "decide", "self", "--reason", "小修", "--files", "src/x.go")
        pre = lambda p: self.hook("pre-tool", {"tool_name": "Edit", "tool_input": {"file_path": p}})  # noqa: E731
        self.assertIsNone(pre(os.path.join(self.other, "src", "x.go")))
        self.assertEqual(pre(os.path.join(self.proj, "src", "x.go"))["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_cross_project_blanket_waiver_stays_in_that_project(self):
        for root in (self.proj, self.other):
            os.makedirs(os.path.join(root, ".claude"), exist_ok=True)
            with open(os.path.join(root, ".claude", "codex-dispatch.json"), "w", encoding="utf-8") as f:
                json.dump({"level": 3}, f)
        self.hook("session-start", {})
        self._cli(self.other, "s1", "decide", "self", "--reason", "整体小修")
        pre = lambda p: self.hook("pre-tool", {"tool_name": "Edit", "tool_input": {"file_path": p}})  # noqa: E731
        self.assertIsNone(pre(os.path.join(self.other, "a.go")))
        self.assertEqual(pre(os.path.join(self.proj, "a.go"))["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_override_in_new_project_binds_to_env_session(self):
        # 在 s1 里给新项目 other 临时调档位；other 里随后开的 s2 不能认走
        self.hook("session-start", {})
        self._cli(self.other, "s1", "override", "--level", "3")
        self.hook("session-start", {"session_id": "s2", "cwd": self.other})
        self.assertEqual(dp.resolve(self.other, "s2")["effective"], 1)
        self.assertEqual(dp.resolve(self.other, "s1")["effective"], 3)

    def _mcp(self, tool="mcp__codex__codex", **tin):
        tin.setdefault("prompt", "x")
        return self.hook("post-tool", {"tool_name": tool, "tool_input": tin, "tool_response": {"content": []}})

    def test_mcp_codex_counts_as_dispatch(self):
        self.proj_cfg({"level": 1})
        self.hook("session-start", {})
        with open(os.path.join(self.proj, "stray.go"), "w") as f:       # 不是这次只读调用改的
            f.write("package x\n")
        self.assertIsNone(self.hook("post-tool", {"tool_name": "mcp__codex__codex",
                                                  "tool_input": {"prompt": "x", "sandbox": "read-only", "cwd": self.proj},
                                                  "tool_response": json.dumps({"threadId": "t1", "content": "ok"})}))
        self.assertIsNone(self._mcp("mcp__codex__codex-reply", threadId="t1"))      # 续聊不算新派活，沿用只读档
        self.assertIn("Codex 派活 1 次", self.hook("stop", {})["systemMessage"])
        c = dp.load_state("s1", self.proj)["codex"]
        self.assertEqual((c["runs"], c["read_runs"], c["files"], c.get("replies")), (1, 1, 0, 1))

    def test_mcp_write_changes_not_blamed_on_claude(self):
        self.proj_cfg({"level": 3})
        self.hook("session-start", {})
        with open(os.path.join(self.proj, "by_codex.go"), "w") as f:      # Codex 在 MCP 里写的
            f.write("package x\n")
        self._mcp(sandbox="workspace-write", cwd=self.proj)
        self.assertIsNone(self.hook("post-tool", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": {}}))
        msg = self.hook("stop", {})["systemMessage"]
        self.assertIn("Codex 派活 1 次", msg)
        self.assertNotIn("命令行改", msg)

    def test_mcp_write_into_other_repo_counts_for_session(self):
        self.proj_cfg({"level": 1})
        self.hook("session-start", {})
        with open(os.path.join(self.other, "n.go"), "w") as f:
            f.write("package n\n")
        self._mcp(sandbox="workspace-write", cwd=self.other)
        self.assertIsNone(self.hook("post-tool", {"cwd": self.other, "tool_name": "Bash",
                                                  "tool_input": {"command": "ls"}, "tool_response": {}}))
        self.assertIn("Codex 派活 1 次", self.hook("stop", {})["systemMessage"])

    def test_readonly_mcp_does_not_swallow_claude_shell_edit(self):
        # 档位 3：Claude 的命令行改动与只读 MCP 并行，MCP 的钩子先跑——改动仍要在命令的钩子里报出来
        self.proj_cfg({"level": 3})
        self.hook("session-start", {})
        with open(os.path.join(self.proj, "claude_did.go"), "w") as f:
            f.write("package x\n")
        self._mcp(sandbox="read-only", cwd=self.proj)
        out = self.hook("post-tool", {"tool_name": "Bash", "tool_input": {"command": "echo > claude_did.go"}, "tool_response": {}})
        self.assertIn("违规", out["hookSpecificOutput"]["additionalContext"])

    def test_reply_refreshes_thread_repo_not_cwd(self):
        # 会话起于 proj；codex(cwd=other) 开线程，续聊（不带 cwd、顶层 cwd=proj）改了 other ⇒ 不能算 Claude 的
        os.makedirs(os.path.join(self.other, ".claude"))
        with open(os.path.join(self.other, ".claude", "codex-dispatch.json"), "w", encoding="utf-8") as f:
            json.dump({"level": 3}, f)
        self.proj_cfg({"level": 1})
        self.hook("session-start", {})
        self.hook("post-tool", {"tool_name": "mcp__codex__codex", "tool_input": {"prompt": "x", "sandbox": "workspace-write", "cwd": self.other},
                                "tool_response": json.dumps({"threadId": "T1", "content": "ok"})})
        with open(os.path.join(self.other, "by_reply.go"), "w") as f:
            f.write("package o\n")
        self._mcp("mcp__codex__codex-reply", threadId="T1")
        out = self.hook("post-tool", {"cwd": self.other, "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": {}})
        self.assertIsNone(out)

    def test_plugin_style_codex_server_name(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("dispatch_hook", HOOK)
        h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(h)
        for t in ("mcp__codex__codex", "mcp__codex__codex-reply", "mcp__plugin_openai_codex__codex"):
            self.assertTrue(h._CODEX_MCP.match(t), t)
        self.assertFalse(h._CODEX_MCP.match("mcp__codex__other_tool"))
        self.assertEqual(h._thread_id('{"threadId":"abc","content":"x"}'), "abc")
        with open(os.path.join(REPO, "docs", "settings-hooks.json"), encoding="utf-8") as f:
            import re
            m = json.load(f)["hooks"]["PostToolUse"][0]["matcher"]
        for t in ("mcp__codex__codex", "mcp__plugin_openai_codex__codex-reply", "Bash"):
            self.assertTrue(re.fullmatch(m, t), (m, t))

    def test_codex_run_without_env_falls_back_to_project_lookup(self):
        self.proj_cfg({"level": 1})
        self.hook("session-start", {})
        self._record_codex(self.proj)
        self.assertIn("Codex 派活 1 次", self.hook("stop", {})["systemMessage"])

    def test_old_single_snapshot_state_migrates(self):
        self.proj_cfg({"level": 3})
        with open(os.path.join(self.proj, "old.go"), "w") as f:
            f.write("package x\n")
        r = dp.resolve(self.proj)
        with dp.session("s1", self.proj) as st:                # 旧格式状态：git_snapshot 只有一份
            st.pop("git_snapshots", None)
            st["git_snapshot"] = dp.dirty_sources(r)
        self.assertIsNone(self.hook("post-tool", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": {}}))


if __name__ == "__main__":
    unittest.main()
