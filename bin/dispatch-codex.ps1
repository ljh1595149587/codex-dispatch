# 派 Codex 干活，事件流式输出，最终报告落盘。跨项目通用，无项目专有假设。
#
# 用法（-Root 缺省时用当前工作目录作项目根）：
#   $D = (Get-ChildItem "$env:USERPROFILE\.claude\skills\*\bin\dispatch-codex.ps1").FullName
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
    [string]$ResumeThread = '',

    # 推理强度，原样传给 codex -c model_reasoning_effort=<值>。不传就按派活类型给默认值：
    # 审查 high，其余 medium。核心代码、高风险改动手动传 high 或 ultra。
    # 全局 config.toml 的值是 ultra 时会把边角情况挨个防一遍（「防御型编程」），派活不跟全局走。
    [string]$Effort = '',

    # 写模式的受保护路径（逗号分隔的 glob，相对项目根，如 "tests/acceptance/*"）。
    # 验收测试由非实现方写好，Codex 为了让测试通过去改测试本身 = 不合格，脚本直接判退出码 3。
    [Parameter(ParameterSetName = 'Task')]
    [string]$Protect = '',

    # 写模式前置检查（见下文 Invoke-PreCheck）。-SkipCheck 跳过，用了的话验收时 Claude 必须自己跑 check
    [Parameter(ParameterSetName = 'Task')]
    [switch]$SkipCheck,

    # 只做前置检查、不派活：忽略缓存重跑一遍，退出码 0 = 能派写模式，4 = 环境问题
    [Parameter(Mandatory = $true, ParameterSetName = 'Check')]
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path $Root).Path

# -Root 缺省是「当前工作目录」，而后台任务、嵌套 shell 的 cwd 常常不是项目根。
# 实测踩过：cwd 落在本 skill 自己的目录，Codex 只读跑遍了工具链自身、找不到目标
# 文件，烧掉 62k token 才在报告里说「当前工作区不存在该文件」。派活方在后台看不到
# 那行「目录 ...」提示，于是这个失败长得像「Codex 做不到」而不是「派错了树」。
# 静默派错比派失败贵：它会跑完、会花钱、会给出一个自洽的错误结论。
$skillRoot = (Split-Path -Parent $PSScriptRoot).TrimEnd('\')
$rootCmp = $root.TrimEnd('\')
if ($rootCmp -eq $skillRoot -or $rootCmp.StartsWith($skillRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    Write-Host "拒绝：项目根解析到了本 skill 自己的目录" -ForegroundColor Red
    Write-Host "  $root" -ForegroundColor DarkGray
    Write-Host "八成是 -Root 缺省取了当前 cwd。显式传 -Root <项目根> 再来。" -ForegroundColor Yellow
    exit 1
}
# 注意：上面那条不能用「是不是 git 仓库」代替 —— 本 skill 目录自己就是 git 仓库，
# 这条查不出来。两道守卫各拦一类，缺一不可。
if (-not (Test-Path (Join-Path $root '.git'))) {
    Write-Host "拒绝：项目根不是 git 仓库" -ForegroundColor Red
    Write-Host "  $root" -ForegroundColor DarkGray
    Write-Host "本工具的验收纪律全建立在 git 上（工作树检查、diff 验收）。" -ForegroundColor Yellow
    Write-Host "显式传 -Root <项目根>。" -ForegroundColor Yellow
    exit 1
}

$target = Resolve-Path (Join-Path $root $Cwd)

# 审查模式固定只读，不走 $Mode
$sandbox = if ($Mode -eq 'read') { 'read-only' } else { 'workspace-write' }

if (-not $Effort) { $Effort = if ($PSCmdlet.ParameterSetName -eq 'Review') { 'high' } else { 'medium' } }
# 上游接受的值（10-29 传非法值时上游报错列出的）；ultra 是 CLI 自己认的别名，全局 config 里一直这么写、能跑
$efforts = 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'
if ($efforts -cnotcontains $Effort) {
    Write-Host "-Effort 只能是 $($efforts -join ' / ')，收到：$Effort" -ForegroundColor Red
    exit 1
}

# 开写权限前必须有干净的回滚点 —— 不采信「它不会乱改」，只信 git。
if ($PSCmdlet.ParameterSetName -eq 'Task' -and $Mode -eq 'write') {
    # 派活日志例外：登记「派给 Codex」时 dispatch_policy.py 刚往里追加过一条，
    # 它不是任何一方的代码改动，也被排除在 Codex 改动统计之外。
    $dirty = git -C $root status --porcelain --untracked-files=all | Where-Object { $_ -notmatch 'docs/dispatch-log\.md$' }
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
# 原生 exe 有两个来源：Codex 桌面版装的（目录名是哈希，随升级变），以及 npm 包自带的
# （node_modules\@openai\codex-win32-x64\vendor\...\codex.exe）。按版本号取最新的。
# 10-30 实测：原来写死的哈希目录已不存在，兜底取到桌面版的 0.163-alpha，而 npm 那份已是 0.165。
function Get-CodexVersionKey([string]$text) {
    if ($text -notmatch '(\d+)\.(\d+)\.(\d+)(-\S+)?') { return $null }
    # 同号的正式版排在预发布版之后
    [pscustomobject]@{ Version = [version]"$($Matches[1]).$($Matches[2]).$($Matches[3])"; Release = [int](-not $Matches[4]) }
}
function Select-NewestCodex($items) {
    # items: @{ Path; VersionText }，返回版本最高的 Path；没有可用的返回 $null
    $items | ForEach-Object {
        $k = Get-CodexVersionKey $_.VersionText
        if ($k) { [pscustomobject]@{ Path = $_.Path; Version = $k.Version; Release = $k.Release } }
    } | Sort-Object Version, Release -Descending | Select-Object -First 1 -ExpandProperty Path
}
$exes = @(Get-ChildItem "$env:LOCALAPPDATA\OpenAI\Codex\bin" -Recurse -Filter 'codex.exe' -ErrorAction SilentlyContinue)
$npmCodex = Get-Command codex -ErrorAction SilentlyContinue
if ($npmCodex) {
    $exes += @(Get-ChildItem (Join-Path (Split-Path $npmCodex.Source) 'node_modules\@openai') -Recurse -Filter 'codex.exe' -ErrorAction SilentlyContinue)
}
$codex = Select-NewestCodex ($exes | ForEach-Object {
    $v = try { (& $_.FullName --version) -join ' ' } catch { '' }
    @{ Path = $_.FullName; VersionText = $v }
})
if (-not $codex) { Write-Host "找不到 codex 原生 exe（桌面版和 npm 包里都没有）" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- 写模式前置检查
# Codex 的 Windows 沙箱以单独的本机账号运行（codexsandboxoffline，组 CodexSandboxUsers）。
# 装在关了权限继承的目录里的 node / python，PATH 传进去了，程序却 Access denied（10-29 实测）。
# 沙箱跑不了 check，写模式就会变成「Codex 写 → 跑不了 → Claude 跑 → 再派回去」，最后 Claude 全包。
# 所以派写模式前，把项目 .claude/codex-dispatch.json 的 check 在本机和沙箱里各跑一次，按差异判：
#   本机过、沙箱不过 → 环境问题，拒绝（退出码 4）
#   两边都不过     → 测试本来就红（验收测试先写好、等实现），不是环境问题，放行
# 两边都不过时环境也可能坏着，所以先逐个探 check 用到的工具（<工具> --version），同样按差异判。
# 沙箱用 codex sandbox -P :workspace：不经模型、不花额度，和写模式派活同一个账号、同一套权限。
$checkCache = Join-Path $(if ($env:CODEX_DISPATCH_HOME) { $env:CODEX_DISPATCH_HOME } else { Join-Path $env:USERPROFILE '.claude' }) 'codex-dispatch-state\check-cache.json'
$checkTtlHours = 24

function Get-CheckCommand {
    $cfg = Join-Path $root '.claude\codex-dispatch.json'
    if (-not (Test-Path $cfg)) { return '' }
    try { $c = (Get-Content $cfg -Raw -Encoding UTF8 | ConvertFrom-Json).check } catch { return '' }
    if ($c -is [string]) { return $c.Trim() }
    return ''
}

# 本机和沙箱各跑一次同一行命令，返回两边的退出码和沙箱输出
function Invoke-Both($line) {
    $o = Join-Path $env:TEMP 'codex-precheck-out.txt'
    $e = Join-Path $env:TEMP 'codex-precheck-err.txt'
    $run = {
        param($exe, $argLine)
        Remove-Item $o, $e -ErrorAction SilentlyContinue
        $p = Start-Process -FilePath $exe -ArgumentList $argLine -WorkingDirectory $root -NoNewWindow -PassThru `
            -RedirectStandardOutput $o -RedirectStandardError $e
        $null = $p.Handle          # PS 5.1：不先取句柄，-NoNewWindow 下 ExitCode 读出来是空的
        $p.WaitForExit()
        $text = (Get-Content $o, $e -Encoding Default -ErrorAction SilentlyContinue) -join "`n"
        return @($p.ExitCode, $text)
    }
    # 参数整串交给 Start-Process，不经 PS 的原生参数拼接（PS 5.1 会弄坏带引号的参数）
    $h = & $run 'cmd.exe' "/d /s /c `"$line`""
    $s = & $run $codex "sandbox -P :workspace -C `"$root`" -- cmd.exe /d /s /c `"$line`""
    return @{ Host = $h[0]; Sandbox = $s[0]; SandboxText = $s[1] }
}

function Get-CheckTools($line) {
    $builtin = 'cd', 'set', 'echo', 'call', 'exit', 'if', 'for', 'rem', 'pushd', 'popd', 'cmd', 'start',
               'del', 'copy', 'move', 'type', 'dir', 'md', 'mkdir', 'rd', 'cls'
    return @($line -split '&&|\|\||&|\|' | ForEach-Object { ($_.Trim() -split '\s+')[0] } |
        Where-Object { $_ -and ($builtin -notcontains $_.ToLower()) } | Select-Object -Unique)
}

function Show-EnvProblem($what, $r) {
    Write-Host "拒绝派写模式：$what 在本机能跑（退出码 $($r.Host)），在 Codex 沙箱里不行（退出码 $($r.Sandbox)）。" -ForegroundColor Red
    Write-Host "沙箱输出（最后 15 行）：" -ForegroundColor DarkGray
    ($r.SandboxText -split "`n" | Select-Object -Last 15) | ForEach-Object { Write-Host "  $_" }
    Write-Host "多半是沙箱账号（组 CodexSandboxUsers）读不到某个安装目录：icacls <目录> 看有没有这个组；" -ForegroundColor Yellow
    Write-Host "补权限：icacls <目录> /grant 'CodexSandboxUsers:(OI)(CI)RX'。修好前要派就加 -SkipCheck，验收时自己跑 check。" -ForegroundColor Yellow
}

# 返回 0 = 能派，4 = 环境问题
function Invoke-PreCheck([bool]$Force) {
    $check = Get-CheckCommand
    if (-not $check) {
        Write-Host "项目没配 check（.claude/codex-dispatch.json 的 check 字段），跳过前置检查。档位 2 的项目应该配上。" -ForegroundColor Yellow
        return $(if ($Force) { 1 } else { 0 })
    }
    if ($check.Contains('"')) {
        Write-Host "check 里不能有双引号（要整串交给 cmd /s /c）：$check" -ForegroundColor Red
        return 1
    }
    $cfgToml = Join-Path $env:USERPROFILE '.codex\config.toml'
    $stamp = if (Test-Path $cfgToml) { (Get-Item $cfgToml).LastWriteTimeUtc.Ticks } else { 0 }
    $key = $root.ToLower()
    $cache = @{}
    if (Test-Path $checkCache) {
        try { (Get-Content $checkCache -Raw -Encoding UTF8 | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $cache[$_.Name] = $_.Value } } catch { }
    }
    $hit = $cache[$key]
    if (-not $Force -and $hit -and $hit.check -eq $check -and $hit.config -eq $stamp -and
        ([DateTime]::UtcNow - [DateTime]::Parse($hit.at).ToUniversalTime()).TotalHours -lt $checkTtlHours) {
        Write-Host "前置检查：$($hit.at) 已通过，24 小时内复用（-CheckOnly 可强制重跑）" -ForegroundColor DarkGray
        return 0
    }

    Write-Host "前置检查：本机和 Codex 沙箱各跑一次 check：$check" -ForegroundColor Cyan
    foreach ($t in Get-CheckTools $check) {
        $r = Invoke-Both "$t --version"
        if ($r.Host -eq 0 -and $r.Sandbox -ne 0) { Show-EnvProblem "工具 $t" $r; return 4 }
    }
    $r = Invoke-Both $check
    if ($r.Host -eq 0 -and $r.Sandbox -ne 0) { Show-EnvProblem 'check' $r; return 4 }
    if ($r.Host -ne 0) {
        Write-Host "check 在本机也不通过（退出码 $($r.Host)），沙箱里 $($r.Sandbox)：测试本身是红的，不是环境问题，放行。" -ForegroundColor Yellow
    } else {
        Write-Host "前置检查通过：check 在 Codex 沙箱里能跑。" -ForegroundColor Green
    }
    $cache[$key] = @{ check = $check; config = $stamp; at = [DateTime]::UtcNow.ToString('o') }
    New-Item -ItemType Directory -Force -Path (Split-Path $checkCache) | Out-Null
    [System.IO.File]::WriteAllText($checkCache, ($cache | ConvertTo-Json -Depth 4), [System.Text.UTF8Encoding]::new($false))
    return 0
}

if ($PSCmdlet.ParameterSetName -eq 'Check') {
    exit (Invoke-PreCheck $true)
}
if ($PSCmdlet.ParameterSetName -eq 'Task' -and $Mode -eq 'write') {
    if ($SkipCheck) {
        Write-Host "跳过前置检查（-SkipCheck）：验收时 Claude 必须自己跑 check，并记进派活日志。" -ForegroundColor Yellow
    } else {
        $pc = Invoke-PreCheck $false
        if ($pc -ne 0) { exit $pc }
    }
}

$raw = Join-Path $root '.codex-stream.jsonl'
$last = Join-Path $root 'codex_report.md'
# codex 的 stderr 原来直接丢弃（2>NUL），参数错误、启动失败全被吞掉，只剩「事件流为空」。
# 落到项目外的临时文件：不进工作树（写模式要求干净），没出报告时把尾部打出来
$errFile = Join-Path ([IO.Path]::GetTempPath()) "codex-dispatch-stderr-$PID.txt"

# 上游没容量 / 限流：看事件流和 stderr。必须先转成字符串 —— 空文件的 Get-Content -Raw 不是 ''，
# 对它做 -notmatch 得到空集合（假），空事件流就被当成「没容量」去重试。
# 10-30 实测：续接参数错误时事件流为空，被这样误报成「上游没容量」两次，误导了一轮判断。
function Test-UpstreamBusy($streamPath, $stderrPath) {
    $text = ''
    foreach ($f in @($streamPath, $stderrPath)) {
        if ($f -and (Test-Path $f)) { $text += [string](Get-Content $f -Raw -Encoding UTF8) }
    }
    return [bool]($text -match 'at capacity|rate limit|overloaded')
}

function Show-NoReport($what, $code) {
    Write-Host "`n$what 没有产出报告（codex 进程退出码 $code）。" -ForegroundColor Red
    if (Test-Path $errFile) {
        $tail = Get-Content $errFile -Encoding UTF8 -Tail 15 | Where-Object { $_ -notmatch 'unrecognized configuration|is ignored\.' }
        if ($tail) { Write-Host 'codex stderr 尾部：' -ForegroundColor Yellow; $tail | ForEach-Object { Write-Host "  $_" } }
    }
    Write-Host "事件流：$raw" -ForegroundColor DarkGray
}

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) {
    $py = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'python.exe' } |
        Where-Object { Test-Path $_ } | Select-Object -Last 1
}
if (-not $py) { Write-Host "找不到 python（事件流过滤需要）" -ForegroundColor Red; exit 1 }

# 只读派活的报告 = Codex 的最后一条回复（-o 存成 codex_report.md），它自己写不了文件。
# 10-29 实测踩过：prompt 写了「报告写进 codex_report.md」，它就用 Set-Content 去写，被只读沙箱拒，
# 最后一条回复只剩一句「无法落盘」，6KB 正文只能从事件流里那条写文件命令中救回来。
# 所以：prompt 像是叫它写文件时告警；并且无条件补一段收尾，写错了也不丢报告。
function Resolve-ReadPrompt([string]$p) {
    $pattern = '(写进|写入|写到|保存到|保存为|存到|存成|落盘到|输出到|write\s+(it\s+)?(in)?to|save\s+(it\s+)?(as|to))\s*[`"''“]?[\w./\\-]+\.(md|txt|json|csv|log)'
    $m = [regex]::Match($p, $pattern, 'IgnoreCase')
    $footer = "`n`n---`n（派活脚本附加）你在只读沙箱里，任何写文件的命令都会失败，不要尝试写文件。" +
        "把完整报告作为你的最后一条回复直接输出，脚本会把它存成 codex_report.md。上面如有「写进某文件」的说法，一律按这条理解。"
    [pscustomobject]@{
        Prompt = $p + $footer
        Warn   = if ($m.Success) { $m.Value } else { '' }
    }
}

function Invoke-Codex {
    param($Sandbox, $Prompt, $OutFile, $Thread = '', $Retries = 3)

    # 先删旧产物：否则「有没有出报告」这个判断会被上一轮的残留污染
    Remove-Item $OutFile -ErrorAction SilentlyContinue

    for ($i = 1; $i -le $Retries; $i++) {
        # 每次重试前清流：否则会读到上一轮残留的 at capacity 而误判
        Remove-Item $raw, $errFile -ErrorAction SilentlyContinue
        $code = Invoke-CodexOnce -Sandbox $Sandbox -Prompt $Prompt -OutFile $OutFile -Thread $Thread
        if (Test-Path $OutFile) { return $code }

        # 「上游没容量」是间歇性的，实测反复出现（5 次）。自动重试，
        # 不要让调用方以为是自己的 prompt 有问题。
        if (-not (Test-UpstreamBusy $raw $errFile)) { return $code }
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

    # exec resume 不认 -s / -C（0.163、0.165 实测：参数错误只进 stderr，事件流为空，看起来像「没产出」）。
    # 沙箱改用 -c sandbox_mode=，工作目录在 cmd 里 cd /d 过去
    $a = if ($Thread) {
        @('-c', "model_reasoning_effort=$Effort", '-c', "sandbox_mode=$Sandbox", 'exec', 'resume', $Thread, '--json', '-o', $OutFile, '-')
    } else {
        @('-c', "model_reasoning_effort=$Effort", 'exec', '--json', '-s', $Sandbox, '-C', $target, '-o', $OutFile, '-')
    }

    $f = @((Join-Path $PSScriptRoot 'codex_stream.py'))
    if ($Budget -gt 0) { $f += @('--budget', $Budget) }
    $f += @('--tee', $raw)

    # PowerShell 没有 `< file` 输入重定向 —— `<` 是保留字符。用 cmd /c 承接。
    # 注意 /c 后面必须整体加引号，否则 cmd 只吃到第一个词。
    $argLine = ($a | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' '
    $fLine = ($f | ForEach-Object {
        if ("$_" -match '[\s"]') { '"' + ("$_" -replace '"', '\"') + '"' } else { "$_" }
    }) -join ' '

    # 管道和落盘都留在 cmd/python 一侧，不经 PowerShell。
    # PS 会按**控制台代码页**解码原生 exe 的输出 —— 中文 Windows 上是 936，
    # UTF-8 的多字节序列被按 GBK 拆开，非 ASCII 成对损坏。原先用 Tee-Object
    # 落盘，存下来的 JSONL 有 32 处 U+FFFD、整行解析失败，事后复查时证据已经烂了。
    # 报告文件由 codex 自己写，不受影响，所以这个损坏只在「回头查事件流」时才暴露。
    $cd = if ($Thread) { 'cd /d "' + $target + '" && ' } else { '' }
    $cmdLine = $cd + '"' + $codex + '" ' + $argLine + ' <"' + $pf + '" 2>"' + $errFile + '" | "' + $py + '" -u ' + $fLine

    # 不要在 PowerShell 层写 `2>&1` —— PS 5.1 会把原生 exe 的每行 stderr 包成
    # NativeCommandError，叠加 $ErrorActionPreference='Stop' 会让一行无害提示
    # （"Reading additional input from stdin..."）直接终止脚本。
    # stderr 在 cmd 层就丢弃，事件流只走 stdout。
    #
    # 过滤器的输出用 Out-Host 直通终端：走管道会被 PS 缓冲，实测失败信息
    # （如 "at capacity"）在任务结束前完全看不到，等于白建这个过滤器。
    & cmd.exe /c $cmdLine | Out-Host

    # 送达自检：prompt 丢失过四轮而全程无报错，只表现为「它没照做」。
    # 用 input_tokens 兜底 —— prompt 少几百 token 看不出来，但整段丢失看得出来。
    $ev = if (Test-Path $raw) { [System.IO.File]::ReadAllText($raw, [System.Text.UTF8Encoding]::new($false)) } else { '' }
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

    Write-Host "第 1 轮：找问题 | 强度 $Effort | $Review" -ForegroundColor Cyan
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
        if (Test-UpstreamBusy $raw $errFile) {
            Write-Host "第 1 轮失败：上游模型没容量，与任务无关。稍后重跑同一条命令即可。" -ForegroundColor Yellow
        } else {
            Show-NoReport '第 1 轮' $code
        }
        exit 5
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
    & $py (Join-Path $PSScriptRoot 'dispatch_policy.py') --cwd $root record-codex --mode read | Out-Null
    exit $code
}

# ---- 普通任务 ----
Remove-Item $raw -ErrorAction SilentlyContinue
Write-Host "档位 $sandbox | 强度 $Effort | 目录 $target" -ForegroundColor Cyan
if ($Mode -eq 'read') {
    $rp = Resolve-ReadPrompt $Prompt
    if ($rp.Warn) {
        Write-Host "告警：只读派活的 prompt 叫 Codex 写文件（「$($rp.Warn)」）。它在只读沙箱里写不了；" -ForegroundColor Yellow
        Write-Host "      已在 prompt 末尾补上「把报告作为最后一条回复输出」。下次直接这么写。" -ForegroundColor Yellow
    }
    $Prompt = $rp.Prompt
}
$code = Invoke-Codex -Sandbox $sandbox -Prompt $Prompt -OutFile $last -Thread $ResumeThread

$policy = Join-Path $PSScriptRoot 'dispatch_policy.py'
if ($Mode -eq 'write') {
    Write-Host "`n=== 改动（自行逐行验收，不采信它的完成报告）===" -ForegroundColor Yellow
    git -C $root --no-pager diff --stat
    # 计入本会话的「Codex 改了多少行」统计（会话结束汇总里和 Claude 的改动对照）
    & $py $policy record-codex-from-git --root $root | Out-Host
    if ($Protect) {
        $bad = & $py $policy check-protected --root $root --globs $Protect
        if ($LASTEXITCODE -eq 1) {
            Write-Host "`n不合格：改了受保护路径（$Protect）：" -ForegroundColor Red
            $bad | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
            Write-Host "验收测试由非实现方编写，Codex 不得修改。回滚后重派，并在需求里写明不许动这些文件。" -ForegroundColor Yellow
            exit 3
        }
    }
} else {
    & $py $policy --cwd $root record-codex --mode read | Out-Null
}

if (-not (Test-Path $last)) {
    # 10-30 实测：续接参数错误时 codex 一行没写、事件流为空，脚本却退出 0，被当成「上游没容量」误判了一轮
    Show-NoReport 'Codex' $code
    exit 5
}
Write-Host "`n报告：$last" -ForegroundColor Green
Write-Host "原始事件流：$raw"
Write-Host "验收完记得往 docs/dispatch-log.md 追一条。" -ForegroundColor DarkGray

exit $code
