# codex-dispatch

派 Codex 干活的 CLI。Windows / PowerShell 环境，跨项目通用。

本仓库同时是一个 Claude Code skill（`SKILL.md` 在根目录），装在
`~/.claude/skills/codex-dispatch/`。skill 加载时读 `SKILL.md`，人和 CLI 用户读本文件。

Claude 用它把「读多写少的探索」和「有机器可判定验收标准的机械活」外包给 Codex，
自己保留判断和验收。**用户不和 Codex 打交道** —— 驾驭它的成本全部落在 Claude 这边。

## 装

不用装。脚本自带依赖检测（codex 原生 exe、python），路径写死在脚本里、找不到会自动搜。

```powershell
$D = "$env:USERPROFILE\.claude\skills\codex-dispatch\bin\dispatch-codex.ps1"
```

## 用

在**项目根目录**执行（或用 `-Root` 指定）：

```powershell
# 只读探索：读多写少的活，用它的额度换我的上下文
& $D -Mode read -Prompt "梳理 X 模块的数据流，报告 ≤4KB"

# 开写权限：描述短、工作量大、有验收命令能判定成败的活
& $D -Mode write -Cwd src -Prompt "给 parsers 补测试" -Budget 200000

# 两轮代码审查：找 → 攻
& $D -Review "src/store.py src/stats.py"

# 只重跑第 2 轮（上游 at capacity 时常用）
& $D -Review "" -Round2Only
```

产出物落在项目根，**记得加进 .gitignore**：

```
codex_report.md
codex_review_find.md
codex_review_attack.md
.codex-stream.jsonl
.codex-prompt.txt
```

## 三档 sandbox

| 档位 | 参数 | 用途 |
|---|---|---|
| 侦查（默认） | `-Mode read` | 找根因、审 diff、读陌生代码、独立复核 |
| 苦力 | `-Mode write` + 尽量窄的 `-Cwd` | 描述短、工作量大、有机器可判定验收标准 |
| — | `danger-full-access` | **不提供** |

开写权限前脚本会检查工作树是否干净，不干净直接拒绝 —— 不采信「它不会乱改」，只信 git。

## 两轮审查为什么是两轮

`/code-review` 的四个机制里，三个能复制：结构化产出、排序、对抗性验证轮。
（第四个是 harness 渲染，学不来。）

**第 2 轮必须开新线程，不能用 `codex-reply`。** 同一上下文里「推翻自己」是走过场 ——
它已经为那些结论背过书了。新线程只喂第 1 轮的表格、不喂推理过程。

第 2 轮的 prompt 里有三条来自实测的硬约束，去掉任何一条都会退化成复述：

1. **禁读既有结论**（`-AnswerKey`，默认 `docs/memory/`）—— 实测它跑去读了项目自己记的
   同一批缺陷的结论，那不是独立复核是照答案抄
2. **强制贴出读到的代码** —— 否则「默认立场是不成立」没有代码支撑，会变成省事的捷径
3. **给填空模板** —— 只说格式要求它会自由发挥，把原报告重述一遍

判成立不等于走过场。看它有没有产出**原报告里没有的东西**（改对行号、补出更严重的分支）。

## 环境事实（Windows / PowerShell 5.1）

这些坑与任务无关、与平台有关，每条都实际踩过。`tests\selfcheck.ps1` 里有对应断言。

| 坑 | 表现 | 处理 |
|---|---|---|
| **`cmd /c` 在第一个换行截断整条命令行** | 多行 prompt 只有第一行到达，**全程无报错**，看起来像模型不照做 | prompt 走 stdin（`codex exec -`） |
| PS 5.1 按 GBK 解码无 BOM 的 UTF-8 | 中文注释的字节被拆出 `}` `"` 等 token，**报错行号与源码完全不对应** | 带中文的 `.ps1` / `AGENTS.md` 必须有 BOM |
| npm 的 `codex.ps1` 包装抢 stdin | 停在 "Reading additional input from stdin..." | 直接调原生 exe，绕开 `Get-Command codex` |
| `--version` 走快速路径不读 stdin | 用它单测**验不出** stdin 问题 | 测 stdin 必须用 `exec` |
| 对原生 exe 用 `2>&1` | PS 5.1 把每行 stderr 包成 `NativeCommandError`，配 `$ErrorActionPreference='Stop'` 会让一行无害提示终止脚本 | stderr 在 cmd 层丢弃 |
| `Tee-Object` 给首行加 BOM | 过滤器 `startswith("{")` 丢掉 `thread.started` | 过滤器 `lstrip("\ufeff")` |
| PS 管道到原生 exe 用 `$OutputEncoding`（5.1 默认 ASCII） | 非 ASCII 字符被压成 `?` | 测编码相关行为要经文件+重定向，别用管道 |
| PowerShell 没有 `< file` 输入重定向 | `<` 是保留字符 | 用 `cmd /c` 承接 |

**最贵的一条是第一条。** 它静默失败四轮，每一轮我都归因成模型行为问题
（「它偷懒」「它复述」），还据此改了四遍措辞、得出过关于模型倾向的结论 ——
而那些措辞一个字都没送到。

**判「它没照做」之前，先证明要求送达了。** 看 `input_tokens` 和
`reasoning_output_tokens`：prompt 整段丢失时 input 明显偏低，真做了核对不可能
只推理十几个 token。脚本已内建送达自检。

## 自检

```powershell
powershell -NoProfile -File tests\selfcheck.ps1
```

19 项，全离线 —— 不调 Codex、不花额度、不联网。**改脚本后先跑这个。**

其中「命令行参数路径确实会截断」是**回归哨兵**：它断言坏行为仍然存在。
若哪天它失败了，说明 cmd 行为变了，可以重新评估实现 —— 但别默默放宽。

## 派活纪律

- **验收不采信它的完成报告**，只看 `git diff` 和验收命令的实际输出。它声称「未验证」的当未完成
- **一次派清、一次读完的才外包。** 需要来回拉扯的自己做 —— 拉扯烧的是 Claude 的上下文
- **显式给报告字数上限。** 实测倍率只有 3–4 倍，且**天花板由报告长度决定，不由源码长度决定**
- **稳定的约束写 `AGENTS.md`，一次性的才写 prompt。** 实测：prompt 里的格式要求被传输层吃掉了，
  它照样输出合规表格 —— 起作用的是 `AGENTS.md`，它自己会读
- **不该外包**：小改动、需要来回澄清的模糊需求、核心逻辑改动（逐行看的成本 > 自己写）

`templates\AGENTS.md` 是给新项目用的模板，拷进项目根后按需裁剪。硬预算 ≤120 行。

## 派活日志

每次验收完记一条（档位、预期、实际、采纳什么、掺水什么、教训）。
**考虑过但决定自己做的活也记一行** —— 防幸存者偏差。

由 Claude 写，不让 Codex 自评，理由同「不采信它的完成报告」。

改规则的门槛：**同一问题 ≥2 次**才动，单次是噪声。
