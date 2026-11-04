#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 `codex exec --json` 的 JSONL 事件流过滤成人可读的单行进度。

用法：codex exec --json ... | python scripts/codex_stream.py [--budget 150000]

每行输出一条事件，供 Monitor 逐条转成通知。设计原则：
- 只输出「你会据此行动」的事件，不是全量日志
- 失败必须可见 —— 静默不能等同于「还在跑」
- token 用量实时可见，超预算当场能发现（曾有一次烧到 294k 触发压缩，事后才察觉）
"""
import sys
import json
import argparse
import io


def truncate(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def strip_shell_wrapper(cmd):
    """command 字段常是 ['powershell.exe','-NoProfile','-Command','<真正的命令>']，
    只留最后那段有信息量的部分。"""
    if isinstance(cmd, list):
        if not cmd:
            return ""
        tail = cmd[-1]
        # bash -lc / powershell -Command 的实际命令都在末位
        return tail if len(cmd) > 1 else cmd[0]
    return str(cmd)


def extract_notes(src, dst):
    """从已落盘的事件流抽出 Codex 的中间说明（agent_message 全文）和跑过的命令清单。

    两轮审查的第 1 轮会读代码、跑命令、试着复现，复现出来却没写进最终表格的问题只留在
    事件流里。原始事件流动辄几百 KB，整份读太贵 —— 这份摘要是给审查者先读的。
    """
    notes, cmds = [], []
    with io.open(src, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.lstrip("﻿").strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("msg") or d
            if m.get("type") != "item.completed":
                continue
            item = m.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                notes.append(item["text"].strip())
            elif item.get("type") == "command_execution":
                cmds.append((item.get("exit_code"), truncate(strip_shell_wrapper(item.get("command")), 200)))
    with io.open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write("# 第 1 轮的中间说明（从事件流抽取，原始事件流见 .codex-stream-find.jsonl）\n\n")
        for i, n in enumerate(notes, 1):
            f.write("## 说明 %d\n\n%s\n\n" % (i, n))
        f.write("## 跑过的命令（%d 条，退出码非 0 的标 !）\n\n" % len(cmds))
        for i, (code, c) in enumerate(cmds, 1):
            f.write("- [%d]%s %s\n" % (i, " !%s" % code if code not in (0, None) else "", c))
    return len(notes), len(cmds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=0,
                    help="token 预算，超出时输出告警行（0 表示不检查）")
    ap.add_argument("--tee", default="",
                    help="把原始事件行另存一份到此路径（UTF-8）")
    ap.add_argument("--notes", nargs=2, metavar=("STREAM", "OUT"),
                    help="不过滤 stdin：从已落盘的事件流 STREAM 抽出中间说明和命令清单，写到 OUT")
    args = ap.parse_args()

    if args.notes:
        n, c = extract_notes(args.notes[0], args.notes[1])
        print("抽出 %d 段说明、%d 条命令" % (n, c))
        return

    # Windows 默认 GBK，中文命令/消息会编码失败。强制 UTF-8。
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")

    # 落盘由本进程做，不用 PowerShell 的 Tee-Object。
    # 原因：PS 会先按**控制台代码页**解码原生 exe 的输出，中文 Windows 上那是
    # 936 —— UTF-8 的多字节序列被按 GBK 拆开，非 ASCII 字符成对损坏，落盘的
    # JSONL 直接解析失败。事后想复查事件流时才发现证据已经烂了。
    # 全程留在 UTF-8 字节域：cmd 负责管道，python 负责落盘。
    tee = None
    if args.tee:
        tee = io.open(args.tee, "a", encoding="utf-8", newline="\n")
    warned = False
    ncmd = 0

    for line in stdin:
        # PowerShell 的 Tee-Object 会给首行加 BOM，﻿ 开头的行不以 { 开头，
        # 会被下面的判断整行丢掉 —— 而那正好是 thread.started。
        line = line.lstrip("﻿").strip()
        if tee is not None and line:
            tee.write(line + "\n")
            tee.flush()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            # 静默丢弃会让「解析坏了」看起来像「什么都没发生」
            print("[codex] 无法解析的事件行: %s" % truncate(line, 100), file=out, flush=True)
            continue
        m = d.get("msg") or d
        t = m.get("type")

        if t == "thread.started":
            print("[codex] thread %s" % m.get("thread_id", "?")[:8], file=out, flush=True)

        elif t == "item.started":
            item = m.get("item") or {}
            if item.get("type") == "command_execution":
                ncmd += 1
                print("[%d] $ %s" % (ncmd, truncate(strip_shell_wrapper(item.get("command")), 150)),
                      file=out, flush=True)

        elif t == "item.completed":
            item = m.get("item") or {}
            it = item.get("type")
            if it == "agent_message":
                print("… %s" % truncate(item.get("text", ""), 200), file=out, flush=True)
            elif it == "command_execution":
                code = item.get("exit_code")
                if code not in (0, None):
                    print("  ! 命令退出码 %s" % code, file=out, flush=True)

        elif t == "error":
            msg = m.get("message", "")
            print("[codex] 错误: %s" % truncate(msg, 200), file=out, flush=True)
            # 上游容量/额度问题不是任务失败，重试即可 —— 但必须当场看见，
            # 否则调用方只会看到「没有产出报告」，去猜是自己的 prompt 有问题。
            if "at capacity" in msg or "rate limit" in msg.lower():
                print("[codex] ↑ 这是上游容量问题，与任务本身无关，稍后重试。",
                      file=out, flush=True)

        elif t == "turn.failed":
            err = m.get("error") or {}
            print("[codex] FAILED: %s" % truncate(err.get("message", ""), 200), file=out, flush=True)

        elif t == "turn.completed":
            u = m.get("usage") or {}
            total = (u.get("input_tokens", 0) or 0) + (u.get("output_tokens", 0) or 0)
            flag = ""
            if args.budget and total > args.budget:
                flag = "  ⚠ 超预算 %s" % args.budget
            print("[codex] 完成 in=%s out=%s 共%s tok，%d 条命令%s"
                  % (u.get("input_tokens"), u.get("output_tokens"), total, ncmd, flag),
                  file=out, flush=True)
            continue

        # 中途预算告警：turn 结束前就要能看见，超了才有机会 kill
        if args.budget and not warned:
            u = m.get("usage") or {}
            if u:
                total = (u.get("input_tokens", 0) or 0) + (u.get("output_tokens", 0) or 0)
                if total > args.budget:
                    warned = True
                    print("[codex] 预算告警：已用 %s tok，超过 %s" % (total, args.budget),
                          file=out, flush=True)

    if tee is not None:
        tee.close()


if __name__ == "__main__":
    main()
