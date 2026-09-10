#Requires -Version 7.0
<#
.SYNOPSIS
    基元律动（TokenRhythm）控制台 API 会话助手。

.DESCRIPTION
    用账号密码登录 tokenrhythm.studio，维护会话 cookie（tr_session / tr_csrf），
    并封装好带 CSRF 头的 API 调用（含非 GET 请求必需的浏览器特征头）。

    安全约定（硬约束）：
    - 脚本不保存任何账号、密码或 API Key 到磁盘、日志或记忆；
    - 只在系统临时目录写一个 cookie jar，调用 Remove-TrSession 即删除；
    - 仅封装站内既有接口，不做并发压测、扫描或任何越权尝试。

.EXAMPLE
    . .\tr-session.ps1
    $s = New-TrSession -Account '<账号>' -Password '<密码>'
    (Invoke-TrApi -Session $s -Path '/api/wallet/summary').data.availableBalanceCny
    Remove-TrSession -Session $s
#>

function Get-TrCsrf {
    <# 从 cookie jar 读取当前 tr_csrf 值（服务端可能轮换，每次调用前重读）。 #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Session)
    $line = Select-String -LiteralPath $Session.Jar -Pattern 'tr_csrf' -SimpleMatch -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if (-not $line) { throw 'cookie jar 中没有 tr_csrf，请重新 New-TrSession。' }
    return ($line.Line -split "`t")[-1]
}

function Invoke-TrApi {
    <#
    .SYNOPSIS
        调用 tokenrhythm.studio 控制台 API（自动带会话 cookie 与 CSRF 头）。
    .PARAMETER Path
        以 / 开头的接口路径，例如 /api/wallet/summary。
    .PARAMETER Body
        请求体（hashtable/PSCustomObject 或已经是 JSON 字符串）。
    .PARAMETER Raw
        返回原始响应文本而不是解析后的对象。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Session,
        [Parameter(Mandatory)] [string] $Path,
        [ValidateSet('GET', 'POST', 'PUT', 'PATCH', 'DELETE')] [string] $Method = 'GET',
        $Body,
        [switch] $Raw
    )

    $tmp = [System.IO.Path]::GetTempPath()
    $outFile = Join-Path $tmp ('tr_out_{0}.json' -f [guid]::NewGuid().ToString('N'))
    $curlArgs = @(
        '-s', '-o', $outFile, '-w', '%{http_code}',
        '-b', $Session.Jar, '-c', $Session.Jar,
        '-H', "Origin: $($Session.BaseUrl)",
        '-H', "Referer: $($Session.BaseUrl)/account"
    )

    $tmpBody = $null
    if ($PSBoundParameters.ContainsKey('Body') -and $null -ne $Body) {
        $tmpBody = Join-Path $tmp ('tr_body_{0}.json' -f [guid]::NewGuid().ToString('N'))
        $json = if ($Body -is [string]) { $Body } else { $Body | ConvertTo-Json -Compress -Depth 8 }
        [System.IO.File]::WriteAllBytes($tmpBody, [System.Text.Encoding]::UTF8.GetBytes($json))
        $curlArgs += @('-X', $Method, '-H', 'Content-Type: application/json; charset=utf-8',
                       '--data-binary', "@$tmpBody")
    }
    elseif ($Method -ne 'GET') {
        $curlArgs += @('-X', $Method)
    }

    if ($Method -ne 'GET') {
        # 关键：缺少这组浏览器特征头时，服务端 CSRF 中间件会直接 403 CSRF_INVALID。
        $curlArgs += @(
            '-H', "X-CSRF-Token: $(Get-TrCsrf -Session $Session)",
            '-H', 'Sec-Fetch-Site: same-origin',
            '-H', 'Sec-Fetch-Mode: cors',
            '-H', 'Sec-Fetch-Dest: empty',
            '-H', 'Accept: */*',
            '-H', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        )
    }

    $status = (& curl.exe @curlArgs "$($Session.BaseUrl)$Path") -join ''
    $text = if (Test-Path -LiteralPath $outFile) { Get-Content -LiteralPath $outFile -Raw -Encoding utf8 } else { '' }
    Remove-Item -LiteralPath $outFile -Force -ErrorAction SilentlyContinue
    if ($tmpBody) { Remove-Item -LiteralPath $tmpBody -Force -ErrorAction SilentlyContinue }

    if ($Raw) { return $text }
    $obj = $null
    try { $obj = $text | ConvertFrom-Json } catch { throw "响应不是合法 JSON（HTTP $status）：$text" }
    if ($status -notmatch '^2') { throw "HTTP $status：$($obj.code) $($obj.message)" }
    return $obj
}

function New-TrSession {
    <#
    .SYNOPSIS
        账号密码登录并返回会话对象（含临时 cookie jar 路径）。
    .OUTPUTS
        PSCustomObject：BaseUrl / Jar / Account / User
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Account,
        [Parameter(Mandatory)] [string] $Password,
        [string] $BaseUrl = 'https://tokenrhythm.studio'
    )

    $tmp = [System.IO.Path]::GetTempPath()
    $jar = Join-Path $tmp ('tr_session_{0}.txt' -f [guid]::NewGuid().ToString('N'))
    $bodyFile = Join-Path $tmp ('tr_login_{0}.json' -f [guid]::NewGuid().ToString('N'))
    $outFile = Join-Path $tmp ('tr_login_out_{0}.json' -f [guid]::NewGuid().ToString('N'))

    $payload = @{ account = $Account; password = $Password } | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllBytes($bodyFile, [System.Text.Encoding]::UTF8.GetBytes($payload))

    $null = & curl.exe -s -o $outFile -c $jar `
        -H 'Content-Type: application/json' `
        -H "Origin: $BaseUrl" -H "Referer: $BaseUrl/login" `
        --data-binary "@$bodyFile" "$BaseUrl/api/auth/login"

    $raw = if (Test-Path -LiteralPath $outFile) { Get-Content -LiteralPath $outFile -Raw -Encoding utf8 } else { '' }
    Remove-Item -LiteralPath $bodyFile, $outFile -Force -ErrorAction SilentlyContinue

    $resp = $raw | ConvertFrom-Json
    if ($resp.code -ne 0) {
        Remove-Item -LiteralPath $jar -Force -ErrorAction SilentlyContinue
        throw "登录失败（$($resp.code)）：$($resp.message)"
    }

    $session = [pscustomobject]@{
        BaseUrl = $BaseUrl
        Jar     = $jar
        Account = $Account
        User    = $resp.data.user
    }
    # 再打一次 /auth/me：让服务端把会话绑定到 tr_session，后续写操作才不会被判定为 CSRF 失效。
    $null = Invoke-TrApi -Session $session -Path '/api/auth/me'
    return $session
}

function Test-TrModel {
    <#
    .SYNOPSIS
        测试指定模型的连通性（走站内 Playground，用会话鉴权，不需要 API Key）。

    .DESCRIPTION
        对目标模型发一次真实推理探针，返回耗时、回复内容、Token 用量与花费。
        默认只做非流式探针；加 -Stream 会再补一次流式连通测试。
        探针式单次调用，不要用来循环压测。

    .PARAMETER Model
        模型 ID，例如 deepseek-v4.1-flash。用 GET /api/models 查准确 ID。

    .PARAMETER Prompt
        探针提示词，默认让模型回四个字，便于肉眼确认返回是否正常。

    .OUTPUTS
        PSCustomObject：
        Model / Ok / LatencyMs / Content / InputTokens / OutputTokens / CostCny /
        TraceId / StreamOk / StreamChunks / Error
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Session,
        [Parameter(Mandatory)] [string] $Model,
        [string] $Prompt = '只回复四个字：连通正常',
        [switch] $Stream
    )

    $result = [ordered]@{
        Model        = $Model
        Ok           = $false
        LatencyMs    = $null
        Content      = $null
        InputTokens  = $null
        OutputTokens = $null
        CostCny      = $null
        TraceId      = $null
        StreamOk     = $null
        StreamChunks = $null
        Error        = $null
    }
    $errors = [System.Collections.Generic.List[string]]::new()

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $r = Invoke-TrApi -Session $Session -Path '/api/playground/send' -Method POST -Body @{
            model    = $Model
            messages = @(@{ role = 'user'; content = $Prompt })
            stream   = $false
        }
        $sw.Stop()
        $d = $r.data
        $result.Ok           = $true
        $result.LatencyMs    = $sw.ElapsedMilliseconds
        $result.Content      = $d.message.content
        $result.InputTokens  = $d.usage.inputTokens
        $result.OutputTokens = $d.usage.outputTokens
        $result.CostCny      = $d.costCny
        $result.TraceId      = $d.traceId
    }
    catch {
        $sw.Stop()
        $result.LatencyMs = $sw.ElapsedMilliseconds
        $errors.Add("非流式探针失败：$($_.Exception.Message)")
    }

    if ($Stream -and $result.Ok) {
        try {
            $raw = Invoke-TrApi -Session $Session -Path '/api/playground/send' -Method POST -Raw -Body @{
                model    = $Model
                messages = @(@{ role = 'user'; content = '从1数到3，只输出数字' })
                stream   = $true
            }
            $result.StreamChunks = @($raw -split "`n" | Where-Object { $_.Trim() }).Count
            $result.StreamOk     = [bool]($raw -match 'data:\s*\[DONE\]')
        }
        catch {
            $result.StreamOk = $false
            $errors.Add("流式探针失败：$($_.Exception.Message)")
        }
    }

    if ($errors.Count -gt 0) { $result.Error = ($errors -join ' | ') }
    return [pscustomobject]$result
}

function Remove-TrSession {
    <# 登出并删除临时 cookie jar。 #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Session)
    try { $null = Invoke-TrApi -Session $Session -Path '/api/auth/logout' -Method POST } catch { }
    if ($Session.Jar -and (Test-Path -LiteralPath $Session.Jar)) {
        Remove-Item -LiteralPath $Session.Jar -Force -ErrorAction SilentlyContinue
    }
}
