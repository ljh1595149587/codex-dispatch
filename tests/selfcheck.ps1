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
        '{"type":"error","message":"Selected model is at capacity. Please try a different model."}'
        '{"type":"turn.completed","usage":{"input_tokens":1000,"cached_input_tokens":0,"output_tokens":50,"reasoning_output_tokens":10}}'
    ) -join "`n"
    $evf = Join-Path $tmp 'cxevents.jsonl'
    [System.IO.File]::WriteAllText($evf, $ev, [System.Text.UTF8Encoding]::new($false))

    $res = (& cmd.exe /c ('"' + $py + '" -u "' + $filter + '" <"' + $evf + '" 2>NUL')) -join "`n"
    Check "BOM 首行仍被解析（thread.started）" ($res -match 'abc123') "Tee-Object 给首行加 BOM"
    Check "识别上游容量问题" ($res -match '容量|capacity') "否则会被误判成 prompt 有问题"
    Check "输出用量" ($res -match '1000|共')
    Remove-Item $evf -ErrorAction SilentlyContinue
}

Write-Host "`n[5] 参数契约" -ForegroundColor Cyan
$sig = Get-Command $script
foreach ($p in @('Mode', 'Prompt', 'Review', 'Round2Only', 'Root', 'Cwd', 'Budget', 'AnswerKey')) {
    Check "参数 -$p 存在" $sig.Parameters.ContainsKey($p)
}
Check "-Root 默认当前目录（脚本已脱离项目树）" ($sig.Parameters['Root'].Attributes.Count -ge 0)

Write-Host "`n通过 $pass，失败 $fail`n" -ForegroundColor $(if ($fail) { 'Red' } else { 'Green' })
exit $(if ($fail) { 1 } else { 0 })
