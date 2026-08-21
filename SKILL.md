---
name: codex-dispatch
description: 把任务外包给 Codex 执行（只读探索、批量机械活、两轮代码审查），Claude 负责派活与验收，用户不和 Codex 打交道。当用户说"派给 codex""让 codex 看看""codex 审查""外包给 codex""用 codex 跑"，或当前任务是"读多写少的探索"/"有验收命令可判定成败的批量活"时使用。触发词：codex、派活、外包、两轮审查、codex review、独立复核
user-invocable: true
allowed-tools: "Bash, Read, Edit, Write, Grep, Glob, PowerShell"
---

# 派 Codex 干活

本 skill 自带 CLI。**先读同目录的 `README.md`** —— 环境坑、派活纪律、两轮审查的
设计理由都在那里，本文件只放调用方式和不变量。

```powershell
$S = "$env:USERPROFILE\.claude\skills\codex-dispatch"
$D = "$S\bin\dispatch-codex.ps1"
```

## 第一原则：用户不和 Codex 打交道

用户和 Claude 交流舒服是因为不用费力表达；驾驭 Codex 让他累。所以
「把模糊意图翻译成 Codex 要的显式约束」这项工作**整个落在 Claude 这边**，不外溢。

具体：用户不该看见 `codex_report.md`、不该看见派活 prompt、不该被问
「它这次改了 4 个文件行不行」。它跑飞了就 revert 重派。

两个隐蔽的泄漏点：

- **不确定性转嫁** —— 说「Codex 说 X 但我没验证」等于把驾驭工作推回给用户。要么验，要么不提
- **拿它的越界找用户仲裁** —— 那是 Claude 的事

## 怎么调

在项目根目录执行（或 `-Root` 指定）：

```powershell
& $D -Mode read -Prompt "梳理 X 的数据流，报告 ≤4KB"      # 侦查
& $D -Mode write -Cwd src -Prompt "补测试" -Budget 200000  # 苦力
& $D -Review "src/a.py src/b.py"                          # 两轮审查
& $D -Review "" -Round2Only                               # 只重跑第 2 轮
```

首次在某项目用之前：把 `$S\templates\AGENTS.md` 拷到项目根，
并把产出物加进 `.gitignore`（清单见 README）。

## 判断要不要外包

**该外包**：读多写少的探索（它的额度换 Claude 的上下文，稳赚）；有测试或命令能判定
成败的批量机械活。

**不该外包**：小改动；需要来回澄清的模糊需求；核心逻辑改动 —— 无论多少行都得逐行看，
自己写更省。

**判据比 token 账更严：一次派清、一次读完的才外包。** 需要来回拉扯的自己做 ——
拉扯烧的是 Claude 的上下文，而那正是用户体验的缓冲垫。

## 验收（不可跳过）

- **不采信它的完成报告**，只看 `git diff` 和验收命令的实际输出
- 它声称「未验证」的部分，**当作未完成处理**
- 开写权限前确认工作树干净（脚本会自动拦）

## 判「它没照做」之前，先证明要求送达了

实测踩过：`cmd /c` 在第一个换行处截断命令行，多行 prompt 只有第一行到达，
全程无报错。表现出来是「它偷懒、它复述」—— 而对着这个不存在的现象改了四轮措辞，
还得出过关于模型行为的结论。

看 `input_tokens` 和 `reasoning_output_tokens`：prompt 整段丢失时 input 明显偏低，
真做了核对不可能只推理十几个 token。**静默的传输故障会伪装成模型的行为问题**，
且后者的「证据」总能自圆其说。

脚本已内建送达自检；走 MCP 时自己看用量。

## 改了脚本就跑自检

```powershell
powershell -NoProfile -File "$env:USERPROFILE\.claude\skills\codex-dispatch\tests\selfcheck.ps1"
```

19 项全离线，不花额度。

## 派活日志

每次验收完往项目的 `docs/dispatch-log.md` 追一条：档位、预期、实际、采纳什么、
掺水什么、验收时发现了它没说的什么、教训。**考虑过但决定自己做的活也记一行**
（防幸存者偏差）。由 Claude 写，不让 Codex 自评。

改规则的门槛：**同一问题 ≥2 次**才动，单次是噪声。
