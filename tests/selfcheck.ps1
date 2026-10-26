# 离线自检：只验传输层和脚本自身，不调用 Codex、不花额度、不联网。
#
# 为什么需要它：`cmd /c` 会在第一个换行处截断整条命令行，多行 prompt 只有
# 第一行到达，全程无报错。表现出来是「模型不照做」—— 而对着这个不存在的
# 现象改了四轮措辞才找到真因。这类静默故障必须由机器判定，不能靠观察输出。
#
# 用法：pwsh -File tests\selfcheck.ps1   （PS 5.1 亦可）
# 退出码 0 = 全过，1 = 有失败。

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo 'bin\dispatch-codex.ps1'
$filter = Join-Path $repo 'bin\codex_stream.py'

$pass = 0
$fail = 0
function Check($desc, $ok, $detail = '') {
    if ($ok) {
        $script:pass++
        Write-Host "  PASS  $desc" -ForegroundColor Green
    } else {
        $script:fail++
        Write-Host "  FAIL  $desc" -ForegroundColor Red
        if ($detail) { Write-Host "        $detail" -ForegroundColor DarkGray }
    }
}

Write-Host "`n[1] 文件编码" -ForegroundColor Cyan
# PS 5.1 按 GBK 解码无 BOM 的 UTF-8，中文注释的字节会被拆出 `}` `"` 等 token，
# 报出的行号和内容与源码完全不对应。带中文的 .ps1 必须有 BOM。
foreach ($f in @($script, (Join-Path $repo 'templates\AGENTS.md'))) {
    $b = [System.IO.File]::ReadAllBytes($f)[0..2]
    Check "$(Split-Path $f -Leaf) 有 UTF-8 BOM" ($b[0] -eq 239 -and $b[1] -eq 187 -and $b[2] -eq 191)
}

Write-Host "`n[2] 语法" -ForegroundColor Cyan
$errs = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($script, [ref]$null, [ref]$errs)
Check "dispatch-codex.ps1 可解析" ($errs.Count -eq 0) ($(if ($errs.Count) { "$($errs[0].Extent.StartLineNumber): $($errs[0].Message)" }))

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) {
    $py = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'python.exe' } |
        Where-Object { Test-Path $_ } | Select-Object -Last 1
}
if ($py) {
    & $py -m py_compile $filter 2>$null
    Check "codex_stream.py 可编译" ($LASTEXITCODE -eq 0)
} else {
    Check "找到 python" $false "事件流过滤依赖 python"
}

Write-Host "`n[3] prompt 送达（核心）" -ForegroundColor Cyan
if ($py) {
    $tmp = [System.IO.Path]::GetTempPath()
    $echo = Join-Path $tmp 'cxecho.py'
    [System.IO.File]::WriteAllText($echo, @'
import sys
d = sys.stdin.buffer.read().decode('utf-8')
sys.stdout.write('%d|%s' % (len(d), d.replace('\n', '\\n')))
'@, [System.Text.UTF8Encoding]::new($false))

    # 多行 + 中文 + 引号 + Markdown 记号，贴近真实 prompt 的形状
    $prompt = @"
第一行：任务说明
第二行：**禁止读 docs/memory/**
第三行："引号" 和 ``反引号``
最后一行：END-MARKER
"@
    $pf = Join-Path $tmp 'cxprompt.txt'
    [System.IO.File]::WriteAllText($pf, $prompt, [System.Text.UTF8Encoding]::new($false))

    $out = & cmd.exe /c ('"' + $py + '" "' + $echo + '" <"' + $pf + '" 2>NUL')
    $got = [int]($out -split '\|')[0]
    Check "多行 prompt 完整送达（$got/$($prompt.Length) 字符）" ($got -eq $prompt.Length) `
        "走 stdin 而非命令行参数；cmd /c 会在第一个换行截断命令行"
    Check "末行标记到达" ($out -like '*END-MARKER*')

    # 反向断言：证明命令行参数那条路确实会截断。这条一旦「失败」，说明
    # 环境行为变了（cmd 不再截断），届时可以放宽实现 —— 但别默默放宽。
    $argEcho = Join-Path $tmp 'cxarg.py'
    [System.IO.File]::WriteAllText($argEcho, "import sys`nsys.stdout.write(str(len(sys.argv[1])))`n",
        [System.Text.UTF8Encoding]::new($false))
    $q = '"' + ($prompt -replace '"', '\"') + '"'
    $argGot = & cmd.exe /c ('"' + $py + '" "' + $argEcho + '" ' + $q + ' <NUL 2>NUL')
    Check "命令行参数路径确实会截断（回归哨兵：收到 $argGot/$($prompt.Length)）" `
        ([int]$argGot -lt $prompt.Length) "若此项失败说明 cmd 行为已变，可重新评估实现"

    Remove-Item $echo, $pf, $argEcho -ErrorAction SilentlyContinue
}

Write-Host "`n[4] 事件流过滤" -ForegroundColor Cyan
if ($py) {
    # Tee-Object 会给首行加 BOM，过滤器若用 startswith("{") 判断会整行丢掉 thread.started。
    # 必须经文件+重定向喂进去，不能用 PS 管道：PS 5.1 的 $OutputEncoding 默认是
    # ASCII，管道送给原生 exe 时 U+FEFF 会被压成 `?`，测的就不是 BOM 了。
    $bom = [char]0xFEFF
    $ev = @(
        "$bom{`"type`":`"thread.started`",`"thread_id`":`"abc123`"}"
        '{"type":"turn.started"}'
        '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"返回类型是 dict 的函数签名"}}'
        '{"type":"error","message":"Selected model is at capacity. Please try a different model."}'
        '{"type":"turn.completed","usage":{"input_tokens":1000,"cached_input_tokens":0,"output_tokens":50,"reasoning_output_tokens":10}}'
    ) -join "`n"
    $evf = Join-Path $tmp 'cxevents.jsonl'
    [System.IO.File]::WriteAllText($evf, $ev, [System.Text.UTF8Encoding]::new($false))

    $res = (& cmd.exe /c ('"' + $py + '" -u "' + $filter + '" <"' + $evf + '" 2>NUL')) -join "`n"
    Check "BOM 首行仍被解析（thread.started）" ($res -match 'abc123') "Tee-Object 给首行加 BOM"
    Check "识别上游容量问题" ($res -match '容量|capacity') "否则会被误判成 prompt 有问题"
    Check "输出用量" ($res -match '1000|共')

    # 落盘编码：PS 按控制台代码页（中文 Windows 是 936）解码原生 exe 的输出，
    # 用 Tee-Object 存下来的 UTF-8 会被按 GBK 拆坏 —— 实测 32 处 U+FFFD、整行
    # 解析失败。落盘必须由 python 自己做，全程留在 UTF-8 字节域。
    $teef = Join-Path $tmp 'cxtee.jsonl'
    Remove-Item $teef -ErrorAction SilentlyContinue
    $null = & cmd.exe /c ('"' + $py + '" -u "' + $filter + '" --tee "' + $teef + '" <"' + $evf + '" 2>NUL')
    $tb = if (Test-Path $teef) { [System.IO.File]::ReadAllBytes($teef) } else { @() }
    $strict = $false
    try { $null = [System.Text.UTF8Encoding]::new($false, $true).GetString($tb); $strict = $tb.Length -gt 0 } catch { }
    Check "--tee 落盘是合法 UTF-8（$($tb.Length)B）" $strict "过滤器自己落盘，不经 PowerShell 管道"
    $tt = if ($tb.Length) { [System.Text.Encoding]::UTF8.GetString($tb) } else { '' }
    Check "--tee 中文原样保留" ($tt -match '返回类型是 dict 的函数签名') "GBK 拆坏时这里会是 U+FFFD"

    # 只匹配真正的调用（行首非注释处的管道调用），注释里提这个名字是允许的 ——
    # 那条注释正是解释为什么不能用它。
    $usesTee = (Get-Content $script -Encoding UTF8 |
        Where-Object { $_ -notmatch '^\s*#' -and $_ -match '\|\s*Tee-Object' }).Count -gt 0
    Check "脚本不再用 Tee-Object 落盘" (-not $usesTee) `
        "PS 管道会按控制台代码页解码，中文被拆坏"

    Remove-Item $evf, $teef -ErrorAction SilentlyContinue
}

Write-Host "`n[5] 参数契约" -ForegroundColor Cyan
$sig = Get-Command $script
foreach ($p in @('Mode', 'Prompt', 'Review', 'Round2Only', 'Root', 'Cwd', 'Budget', 'AnswerKey')) {
    Check "参数 -$p 存在" $sig.Parameters.ContainsKey($p)
}
Check "-Root 默认当前目录（脚本已脱离项目树）" ($sig.Parameters['Root'].Attributes.Count -ge 0)

Write-Host "`n[6] 自引用不变量" -ForegroundColor Cyan
# 改名/搬家的成本全部落在硬编码的自引用上，且它们不报错、只静默失配。
# 上一次搬家（d:\work\ → skills 目录）在 settings.local.json 里留下约 20 条
# 指向死路径的权限条目，至今没清。所以这里禁止任何文件按目录名写死 skill 路径：
# 要引用自身就用 bin\dispatch-codex.ps1 这个文件名去 glob（见 SKILL.md 顶部）。
# 允许 `skills\*\`（glob 形式），禁止 `skills\具体名字\`。
$selfRef = @()
$scan = Get-ChildItem $repo -Recurse -File -Include *.md, *.ps1, *.py |
    Where-Object { $_.FullName -notmatch '[\\/]\.git[\\/]' }
foreach ($f in $scan) {
    $txt = [System.IO.File]::ReadAllText($f.FullName)
    foreach ($m in [regex]::Matches($txt, '\.claude[\\/]skills[\\/][^*\s\\/]+[\\/]')) {
        $selfRef += "$($f.Name) → $($m.Value)"
    }
}
Check "无按目录名硬编码的 skill 路径" ($selfRef.Count -eq 0) ($selfRef -join ' ; ')

Write-Host "`n[7] 派错树的守卫" -ForegroundColor Cyan
# 实测踩过：-Root 缺省取当前 cwd，后台任务的 cwd 落在本 skill 自己的目录，
# Codex 只读跑遍工具链自身、找不到目标文件，烧掉 62k token 才报「工作区没有该文件」。
# 派活方在后台看不到那行「目录 ...」提示，于是失败长得像「Codex 做不到」而非「派错了树」。
# 静默派错比派失败贵：它会跑完、会花钱、会给出一个自洽的错误结论。
$guardTmp = Join-Path ([IO.Path]::GetTempPath()) ("dispatch-guard-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $guardTmp -Force | Out-Null
try {
    # 守卫一：项目根落在 skill 自己的目录 → 必须拒绝。
    # 注意这条不能用「是不是 git 仓库」代替 —— 本 skill 目录自己就是 git 仓库。
    $out = & powershell -NoProfile -File $script -Mode read -Prompt 'x' -Root $repo 2>&1 | Out-String
    Check "项目根 = skill 目录时拒绝" ($LASTEXITCODE -eq 1 -and $out -match '本 skill 自己的目录') $out.Trim()

    # 守卫二：非 git 仓库 → 必须拒绝（验收纪律全建立在 git 上）
    $out = & powershell -NoProfile -File $script -Mode read -Prompt 'x' -Root $guardTmp 2>&1 | Out-String
    Check "项目根不是 git 仓库时拒绝" ($LASTEXITCODE -eq 1 -and $out -match '不是 git 仓库') $out.Trim()

    # 两道守卫都必须在调用 Codex 之前生效，否则拦住了也已经花过钱了
    Check "守卫先于 Codex 调用（未产生事件流）" (-not (Test-Path (Join-Path $guardTmp '.codex-stream.jsonl')))
} finally {
    Remove-Item $guardTmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "`n[8] 档位与钩子（tests\test_policy.py）" -ForegroundColor Cyan
# 档位、额度、拦截、提醒、统计全由钩子在每次工具调用时执行，坏了不会报错，
# 只会静默地「不提醒」「不拦截」—— 正是这套机制要防的那种失效，所以必须机器判定。
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) {
    Check "找到 python" $false
} else {
    # unittest 把结果写到 stderr。不能在 PS 层 2>&1（PS 5.1 会包成 NativeCommandError，
    # 配 $ErrorActionPreference='Stop' 直接终止脚本，见 README 环境事实表），在 cmd 层合流。
    $tdir = Join-Path $repo 'tests'
    $out = & cmd.exe /c "`"$py`" -m unittest discover -s `"$tdir`" -p test_*.py 2>&1" | Out-String
    $ran = if ($out -match 'Ran (\d+) test') { $Matches[1] } else { '?' }
    Check "单测全过（$ran 条）" ($LASTEXITCODE -eq 0) ($out.Trim() -split "`n" | Select-Object -Last 12 | Out-String)
}

Write-Host "`n通过 $pass，失败 $fail`n" -ForegroundColor $(if ($fail) { 'Red' } else { 'Green' })
exit $(if ($fail) { 1 } else { 0 })
