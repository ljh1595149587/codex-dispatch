# 派 Codex 干活，事件流式输出，最终报告落盘。跨项目通用，无项目专有假设。
#
# 用法（-Root 缺省时用当前工作目录作项目根）：
#   $D = 'd:\work\codex-dispatch\bin\dispatch-codex.ps1'
#   & $D -Mode read  -Prompt "复核 X 的结论"
#   & $D -Mode write -Cwd src -Prompt "给 parsers 补测试" -Budget 200000
#   & $D -Review "src/a.py src/b.py"        # 找 → 攻 两轮
#   & $D -Review "" -Round2Only             # 只重跑第 2 轮，复用已有的第 1 轮报告
#
# 与 MCP 调用的分工：短任务（一两轮）用 MCP 更省事；长任务或开写权限时用本脚本，
# 因为 MCP 不流式返回 —— 它内部跑多轮只在最后返回一次，界面上看不到过程。
#
# 产出物落在项目根，记得加进 .gitignore：
#   codex_report.md / codex_review_find.md / codex_review_attack.md
#   .codex-stream.jsonl / .codex-prompt.txt

param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Task')]
    [ValidateSet('read', 'write')]
    [string]$Mode,

    [Parameter(Mandatory = $true, ParameterSetName = 'Task')]
    [string]$Prompt,

    # 审查模式：对指定文件跑「找 → 攻」两轮，第二轮开新线程独立复核第一轮的每条发现。
    # 值原样嵌进 prompt 给 Codex 看。配 -Round2Only 时传空串。
    [Parameter(Mandatory = $true, ParameterSetName = 'Review')]
    [AllowEmptyString()]
    [string]$Review,

    # 只跑第 2 轮，复用已有的 codex_review_find.md。
    # 上游 "at capacity" 是间歇性的，第 1 轮成功第 2 轮挂掉的情况实测出现过多次，
    # 没有这个开关就得连第 1 轮一起重跑。
    [Parameter(ParameterSetName = 'Review')]
    [switch]$Round2Only,

    # 第 2 轮禁读的路径 —— 「答案泄漏」防线。实测踩过：第 2 轮跑去读了
    # docs/memory/ 下我自己记的同一批缺陷的结论，那就不是独立复核而是照抄。
    # 各项目放既有结论的地方不同，所以做成参数。
    [Parameter(ParameterSetName = 'Review')]
    [string]$AnswerKey = 'docs/memory/',

    # 项目根。脚本已挪出项目树，不能再由 $PSScriptRoot 推断。
    [string]$Root = '.',

    # 相对项目根的子目录，收窄可写范围。默认项目根。
    [string]$Cwd = '.',

    # token 预算，超出时流里会打告警行。0 = 不检查。
    [int]$Budget = 0,

    # 续接已有线程而非新开
    [string]$ResumeThread = ''
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path $Root).Path
$target = Resolve-Path (Join-Path $root $Cwd)

# 审查模式固定只读，不走 $Mode
$sandbox = if ($Mode -eq 'read') { 'read-only' } else { 'workspace-write' }

# 开写权限前必须有干净的回滚点 —— 不采信「它不会乱改」，只信 git。
if ($PSCmdlet.ParameterSetName -eq 'Task' -and $Mode -eq 'write') {
    $dirty = git -C $root status --porcelain
    if ($dirty) {
        Write-Host "工作树不干净，先提交或 stash 再开写权限：" -ForegroundColor Red
        Write-Host $dirty
        exit 1
    }
    $head = git -C $root rev-parse --short HEAD
    Write-Host "回滚点 $head — 出问题用 git reset --hard $head" -ForegroundColor Yellow
}

# 优先用原生 exe。`Get-Command codex` 在装了 npm 包的机器上会解析到
# C:\nvm4w\nodejs\codex.ps1 —— 那个包装脚本内部是 `$input | & node ...`，
# 它自己要读 stdin，任何喂给它的东西都会让 node 停在
# "Reading additional input from stdin..."。绕开它。
$codex = "$env:LOCALAPPDATA\OpenAI\Codex\bin\39633208fb6e0c47\codex.exe"
if (-not (Test-Path $codex)) {
    $found = Get-ChildItem "$env:LOCALAPPDATA\OpenAI\Codex\bin" -Recurse -Filter 'codex.exe' -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
    if ($found) { $codex = $found }
}
if (-not (Test-Path $codex)) { Write-Host "找不到 codex 原生 exe" -ForegroundColor Red; exit 1 }

$raw = Join-Path $root '.codex-stream.jsonl'
$last = Join-Path $root 'codex_report.md'

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) {
    $py = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'python.exe' } |
        Where-Object { Test-Path $_ } | Select-Object -Last 1
}
if (-not $py) { Write-Host "找不到 python（事件流过滤需要）" -ForegroundColor Red; exit 1 }

function Invoke-Codex {
    param($Sandbox, $Prompt, $OutFile, $Thread = '', $Retries = 3)

    # 先删旧产物：否则「有没有出报告」这个判断会被上一轮的残留污染
    Remove-Item $OutFile -ErrorAction SilentlyContinue

    for ($i = 1; $i -le $Retries; $i++) {
        # 每次重试前清流：否则会读到上一轮残留的 at capacity 而误判
        Remove-Item $raw -ErrorAction SilentlyContinue
        $code = Invoke-CodexOnce -Sandbox $Sandbox -Prompt $Prompt -OutFile $OutFile -Thread $Thread
        if (Test-Path $OutFile) { return $code }

        # 「上游没容量」是间歇性的，实测反复出现（5 次）。自动重试，
        # 不要让调用方以为是自己的 prompt 有问题。
        $tail = if (Test-Path $raw) { Get-Content $raw -Raw } else { '' }
        if ($tail -notmatch 'at capacity|rate limit') { return $code }
        if ($i -lt $Retries) {
            $wait = 20 * $i
            Write-Host "上游没容量，${wait}s 后重试（$i/$Retries）" -ForegroundColor Yellow
            Start-Sleep -Seconds $wait
        }
    }
    return $code
}

function Invoke-CodexOnce {
    param($Sandbox, $Prompt, $OutFile, $Thread = '')

    # prompt 走 stdin，不走命令行参数。
    # `cmd /c` 在**第一个换行处截断整条命令行** —— 实测 43 字符的三行 prompt
    # 只有第一行的 8 个字符到达进程。多行 prompt 全程静默丢失，表现为
    # 「它没按要求做」，实际是要求根本没送到。四轮改措辞都白改。
    # `codex exec -` 从 stdin 读 prompt，没有长度和换行限制。
    $pf = Join-Path $root '.codex-prompt.txt'
    [System.IO.File]::WriteAllText($pf, $Prompt, [System.Text.UTF8Encoding]::new($false))

    $a = if ($Thread) {
        @('exec', 'resume', $Thread, '--json', '-s', $Sandbox, '-C', $target, '-o', $OutFile, '-')
    } else {
        @('exec', '--json', '-s', $Sandbox, '-C', $target, '-o', $OutFile, '-')
    }

    $f = @((Join-Path $PSScriptRoot 'codex_stream.py'))
    if ($Budget -gt 0) { $f += @('--budget', $Budget) }

    # PowerShell 没有 `< file` 输入重定向 —— `<` 是保留字符。用 cmd /c 承接。
    # 注意 /c 后面必须整体加引号，否则 cmd 只吃到第一个词。
    $argLine = ($a | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' '
    $cmdLine = '"' + $codex + '" ' + $argLine + ' <"' + $pf + '" 2>NUL'

    # 不要在 PowerShell 层写 `2>&1` —— PS 5.1 会把原生 exe 的每行 stderr 包成
    # NativeCommandError，叠加 $ErrorActionPreference='Stop' 会让一行无害提示
    # （"Reading additional input from stdin..."）直接终止脚本。
    # stderr 在 cmd 层就丢弃，事件流只走 stdout。
    #
    # 过滤器的输出用 Out-Host 直通终端：走管道会被 PS 缓冲，实测失败信息
    # （如 "at capacity"）在任务结束前完全看不到，等于白建这个过滤器。
    & cmd.exe /c $cmdLine |
        Tee-Object -FilePath $raw -Append |
        & $py -u @f |
        Out-Host

    # 送达自检：prompt 丢失过四轮而全程无报错，只表现为「它没照做」。
    # 用 input_tokens 兜底 —— prompt 少几百 token 看不出来，但整段丢失看得出来。
    $ev = if (Test-Path $raw) { Get-Content $raw -Raw } else { '' }
    if ($ev -match '"input_tokens":\s*(\d+)') {
        $inTok = [int]$Matches[1]
        $expect = [int]($Prompt.Length / 3)
        if ($inTok -lt $expect) {
            Write-Host "警告：input_tokens=$inTok，低于 prompt 长度($($Prompt.Length) 字符)的下限估计。prompt 可能没送达。" -ForegroundColor Red
        }
    }

    return $LASTEXITCODE
}

# ---- 审查模式：找 → 攻 ----
# 第二轮必须开新线程，不能用 resume。同一上下文里「推翻自己」是走过场 ——
# 它已经为那些结论背过书了。新线程只喂表格、不喂推理过程，它才会真去查代码。
if ($PSCmdlet.ParameterSetName -eq 'Review') {
    Remove-Item $raw -ErrorAction SilentlyContinue

    $r1 = Join-Path $root 'codex_review_find.md'
    $r2 = Join-Path $root 'codex_review_attack.md'

    Write-Host "第 1 轮：找问题 | $Review" -ForegroundColor Cyan
    $p1 = @"
审查这些文件：$Review

严格按 AGENTS.md「代码审查任务的固定格式」输出表格。只报会出错的东西，不报风格问题。
宁可少报 —— 每条都会被独立复核，假阳性比漏报代价大。
"@
    if ($Round2Only) {
        if (-not (Test-Path $r1)) {
            Write-Host "-Round2Only 需要已有的 $r1，但它不存在。先跑完整两轮。" -ForegroundColor Red
            exit 1
        }
        Write-Host "跳过第 1 轮，复用 $r1" -ForegroundColor DarkGray
    } else {
        $code = Invoke-Codex -Sandbox 'read-only' -OutFile $r1 -Prompt $p1
    }

    if (-not (Test-Path $r1)) {
        # 区分「任务失败」和「上游没容量」—— 后者重试即可，别让调用方去改 prompt
        $tail = if (Test-Path $raw) { Get-Content $raw -Raw } else { '' }
        if ($tail -match 'at capacity|rate limit') {
            Write-Host "第 1 轮失败：上游模型没容量，与任务无关。稍后重跑同一条命令即可。" -ForegroundColor Yellow
        } else {
            Write-Host "第 1 轮没有产出报告，中止。详见 $raw" -ForegroundColor Red
        }
        exit 1
    }

    Write-Host "`n第 2 轮：独立复核（新线程）" -ForegroundColor Cyan
    # 不把报告内容拼进命令行 —— cmd /c 有长度上限，超了会静默截断，
    # 表现为第 2 轮「看不到待复核的条目」而空转（实测只吃了 13k token）。
    # 让它自己去读文件。
    # 三条来自实测的约束，缺一条这轮就退化成复述：
    # 1) 禁 docs/memory/ —— 那里有我自己记的同一批缺陷的结论，读了就是照答案抄
    # 2) 强制「贴出读到的代码」—— 否则「默认立场是不成立」没有代码支撑，
    #    会被当成省事的捷径，直接一律判不成立
    # 3) 给填空模板 —— 只说格式要求它会自由发挥，把原报告重述一遍
    $p2 = @"
读取项目根目录的 ``codex_review_find.md``，那是另一位审查者的发现表。
你的任务是核对每一条是否真实存在，不是补充新问题。

**禁止读以下路径：$AnswerKey。** 那里记录着别人对这些问题的既有结论，
读了就不是独立核对，是照答案抄。你的判断只能来自**源码本身**。

**必须逐条执行，不能跳过：**
1. 打开它引用的文件，读出问题所在行的**前后各 20 行**
2. 在回复里**贴出你实际读到的关键几行代码**
3. 基于你读到的代码判断

判定标准：
- 触发条件在真实代码路径下无法达成 → 不成立，说明为什么
- 代码里已有防护 → 不成立，指出防护在哪一行
- 确实存在 → 成立，补上原报告没写清楚的触发细节

**只有你亲自读过那几行代码，才能给出判定。** 没读就写「无法判定」。

每条按此格式输出，原报告有几条就写几条，一条不少：

## <file:line>
读到的代码：
    <贴出关键几行>
判定：成立 / 不成立 / 无法判定
依据：<一句话，带 file:line>

不要复述原报告的内容。
"@
    $code = Invoke-Codex -Sandbox 'read-only' -OutFile $r2 -Prompt $p2

    Write-Host "`n第 1 轮 $r1" -ForegroundColor Green
    Write-Host "第 2 轮 $r2" -ForegroundColor Green
    Write-Host "两份对照读：只采信第 2 轮判成立的。" -ForegroundColor DarkGray
    Write-Host "原始事件流：$raw"
    exit $code
}

# ---- 普通任务 ----
Remove-Item $raw -ErrorAction SilentlyContinue
Write-Host "档位 $sandbox | 目录 $target" -ForegroundColor Cyan
$code = Invoke-Codex -Sandbox $sandbox -Prompt $Prompt -OutFile $last -Thread $ResumeThread

if ($Mode -eq 'write') {
    Write-Host "`n=== 改动（自行逐行验收，不采信它的完成报告）===" -ForegroundColor Yellow
    git -C $root --no-pager diff --stat
}

if (Test-Path $last) {
    Write-Host "`n报告：$last" -ForegroundColor Green
}
Write-Host "原始事件流：$raw"
Write-Host "验收完记得往 docs/dispatch-log.md 追一条。" -ForegroundColor DarkGray

exit $code
