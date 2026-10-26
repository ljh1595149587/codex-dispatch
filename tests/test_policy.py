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

    def tearDown(self):
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

    def hook(self, event, payload):
        payload.setdefault("session_id", "s1")
        payload.setdefault("cwd", self.proj)
        p = subprocess.run([sys.executable, HOOK, event], input=json.dumps(payload).encode("utf-8"),
                           capture_output=True, env=os.environ.copy())
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


if __name__ == "__main__":
    unittest.main()
