#Requires -Version 5.1
<#
.SYNOPSIS
    TokenRhythm（基元律动）生图 / 垫图改图 —— PowerShell 版（Windows 零依赖）

.DESCRIPTION
    不需要 Python、不需要插件：直接用 Windows 自带的 PowerShell 调用
    https://tokenrhythm.studio/v1 接口。Windows PowerShell 5.1 与 PowerShell 7+ 均可运行。

    配置 Key（三选一，优先级从高到低）：
      1) 调用时加 -ApiKey sk_tr_xxx
      2) 设置环境变量 TOKENRHYTHM_API_KEY
      3) 把本文件中部 $ConfiguredApiKey 的引号填上你的 Key   ← 分发时让对方填这里最省事

.EXAMPLE
    .\tr_image.ps1 gen -Prompt "一只戴墨镜的橘猫，赛博朋克霓虹背景"
    .\tr_image.ps1 gen -Prompt "雪山日出" -Size 1280x720 -N 2 -Out .\out
    .\tr_image.ps1 edit -Prompt "把背景换成雪景" -Image .\cat.png

.EXAMPLE
    # 默认执行策略禁止跑脚本时，用这个姿势绕过（不改系统设置）
    powershell -NoProfile -ExecutionPolicy Bypass -File .\tr_image.ps1 gen -Prompt "雪山日出"
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command,

    [Parameter(Position = 1)]
    [string]$Prompt,

    [string]$PromptFile,
    [string]$Image,

    [ValidateSet('qwen-image-2.0', 'wan2.7-image')]
    [string]$Model = 'qwen-image-2.0',

    [ValidateSet('1024x1024', '1280x720', '720x1280', '1280x1280', '1024x1792', '1792x1024')]
    [string]$Size,

    [ValidateRange(1, 4)]
    [int]$N = 1,

    [string]$Out,
    [string]$ApiKey = $env:TOKENRHYTHM_API_KEY,
    [string]$BaseUrl = 'https://tokenrhythm.studio',
    [int]$TimeoutSec = 300,
    [switch]$Json,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

# =============================================================================
# ★★★ 配置区：把 Key 填进下面引号里（形如 sk_tr_xxxx）★★★
#     取 Key：登录 tokenrhythm.studio -> API Keys -> 新建 -> 复制完整 Key
# =============================================================================
$ConfiguredApiKey = ''
# =============================================================================


function Exit-Fail {
    param([string]$Message, [int]$Code = 1)
    [Console]::Error.WriteLine("[ERROR] $Message")
    exit $Code
}


function Show-Usage {
    Write-Output @'
TokenRhythm 生图 / 垫图改图（PowerShell 版，Windows 零依赖）

用法：
  .\tr_image.ps1 gen  -Prompt "提示词" [-Size 1024x1024] [-Model qwen-image-2.0] [-N 1] [-Out 目录] [-Json]
  .\tr_image.ps1 edit -Prompt "改图指令" -Image .\原图.png [-Out 目录]

参数：
  -Prompt / -PromptFile   提示词；提示词很长时用 -PromptFile 指向 UTF-8 文本文件
  -Model    qwen-image-2.0（默认） | wan2.7-image
  -Size     1024x1024（gen 默认）/ 1280x720 / 720x1280 / 1280x1280 / 1024x1792 / 1792x1024
  -N        生成张数 1-4（每张 0.20 元）
  -Image    原图路径，edit 必填，1 张、<=10MB、jpg/png
  -Out      保存目录，默认当前目录
  -Json     输出机器可读 JSON（含 files / cost_cny / trace_id）
  -ApiKey   临时指定 Key；-BaseUrl 自定义站点；-TimeoutSec 超时秒数

执行策略受限时：
  powershell -NoProfile -ExecutionPolicy Bypass -File .\tr_image.ps1 gen -Prompt "雪山日出"
'@
}


function Get-HttpErrorBody {
    param($ErrorRecord)
    $ex = $ErrorRecord.Exception
    if ($null -eq $ex -or $null -eq $ex.Response) { return $null }
    $resp = $ex.Response
    try {
        # PowerShell 7 走 HttpResponseMessage
        if ($resp -is [System.Net.Http.HttpResponseMessage]) {
            return $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        }
        # Windows PowerShell 5.1 走 HttpWebResponse
        $stream = $resp.GetResponseStream()
        $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
        return $reader.ReadToEnd()
    }
    catch { return $null }
}


function Handle-ApiError {
    param($ErrorRecord, [string]$Action)
    $status = 0
    $detail = $ErrorRecord.Exception.Message
    try {
        if ($ErrorRecord.Exception.Response) {
            $status = [int]$ErrorRecord.Exception.Response.StatusCode
        }
    }
    catch { }
    $bodyText = Get-HttpErrorBody -ErrorRecord $ErrorRecord
    if (-not [string]::IsNullOrWhiteSpace($bodyText)) { $detail = $bodyText }
    if ($detail.Length -gt 500) { $detail = $detail.Substring(0, 500) }

    switch ($status) {
        401 { Exit-Fail "$Action 失败 HTTP 401：Key 无效或已删除。响应：$detail" 4 }
        429 { Exit-Fail "$Action 失败 HTTP 429：触发限流，请稍后重试。响应：$detail" 5 }
        0 { Exit-Fail "$Action 失败（网络异常，检查网络或代理）：$detail" 3 }
        default { Exit-Fail "$Action 失败 HTTP $status`: $detail" 6 }
    }
}


function Invoke-TrApiJson {
    param([string]$Url, [hashtable]$BodyObject, [string]$Key, [int]$TimeoutSec)

    $json = $BodyObject | ConvertTo-Json -Depth 6 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $headers = @{ Authorization = "Bearer $Key"; Accept = 'application/json' }
    try {
        return Invoke-RestMethod -Uri $Url -Method Post -Body $bytes `
            -ContentType 'application/json; charset=utf-8' -Headers $headers -TimeoutSec $TimeoutSec
    }
    catch {
        Handle-ApiError -ErrorRecord $_ -Action '接口调用'
    }
}


function Invoke-TrApiMultipart {
    param([string]$Url, [hashtable]$Fields, [string]$ImagePath, [string]$Key, [int]$TimeoutSec)

    $boundary = '----TRBoundary' + [System.Guid]::NewGuid().ToString('N')
    $nl = "`r`n"
    $ms = New-Object System.IO.MemoryStream

    foreach ($name in $Fields.Keys) {
        $part = "--$boundary$nl" +
                "Content-Disposition: form-data; name=`"$name`"$nl$nl" +
                "$($Fields[$name])$nl"
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($part)
        $ms.Write($bytes, 0, $bytes.Length)
    }

    $ext = [System.IO.Path]::GetExtension($ImagePath).ToLower()
    if ($ext -notin @('.png', '.jpg', '.jpeg')) { $ext = '.png' }
    $ctype = if ($ext -eq '.png') { 'image/png' } else { 'image/jpeg' }
    $header = "--$boundary$nl" +
              "Content-Disposition: form-data; name=`"image`"; filename=`"image$ext`"$nl" +
              "Content-Type: $ctype$nl$nl"
    $hbytes = [System.Text.Encoding]::UTF8.GetBytes($header)
    $ms.Write($hbytes, 0, $hbytes.Length)

    $fileBytes = [System.IO.File]::ReadAllBytes($ImagePath)
    $ms.Write($fileBytes, 0, $fileBytes.Length)

    $tail = [System.Text.Encoding]::ASCII.GetBytes("$nl--$boundary--$nl")
    $ms.Write($tail, 0, $tail.Length)
    $payload = $ms.ToArray()
    $ms.Dispose()

    $headers = @{ Authorization = "Bearer $Key"; Accept = 'application/json' }
    try {
        return Invoke-RestMethod -Uri $Url -Method Post -Body $payload `
            -ContentType "multipart/form-data; boundary=$boundary" -Headers $headers -TimeoutSec $TimeoutSec
    }
    catch {
        Handle-ApiError -ErrorRecord $_ -Action '垫图改图'
    }
}


function Get-ImageItems {
    param($Payload)
    $node = $Payload.data
    if ($null -eq $node) { $node = $Payload }
    if ($node -is [string]) { return @($node) }
    if ($node -is [System.Collections.IEnumerable]) {
        $list = @($node)
        # 兼容 { data: { data: [...] } } 形态
        if ($list.Count -eq 1 -and $list[0] -isnot [string] -and $list[0].PSObject.Properties['data']) {
            return @($list[0].data)
        }
        return $list
    }
    return @($node)
}


function Save-Images {
    param($Payload, [string]$OutDir, [string]$Prefix, [int]$TimeoutSec)

    $saved = New-Object System.Collections.Generic.List[string]
    $index = 0
    foreach ($item in (Get-ImageItems -Payload $Payload)) {
        $index++
        $url = $null
        $b64 = $null
        if ($item -is [string]) {
            $url = $item
        }
        elseif ($item.PSObject.Properties['url']) {
            $url = $item.url
        }
        elseif ($item.PSObject.Properties['b64_json']) {
            $b64 = $item.b64_json
        }

        if ($url) {
            $ext = '.png'
            try {
                $candidate = [System.IO.Path]::GetExtension(([Uri]$url).AbsolutePath).ToLower()
                if ($candidate -in @('.png', '.jpg', '.jpeg', '.webp')) { $ext = $candidate }
            }
            catch { }
            $dest = Join-Path $OutDir "$Prefix-$index$ext"
            try {
                $wc = New-Object System.Net.WebClient
                $wc.DownloadFile($url, $dest)
                $wc.Dispose()
            }
            catch {
                Exit-Fail "图片下载失败（链接 24 小时过期，可重新生成）：$($_.Exception.Message)" 7
            }
        }
        elseif ($b64) {
            $dest = Join-Path $OutDir "$Prefix-$index.png"
            try { [System.IO.File]::WriteAllBytes($dest, [Convert]::FromBase64String($b64)) }
            catch { Exit-Fail "base64 图片解码失败：$($_.Exception.Message)" 8 }
        }
        else {
            continue
        }
        $saved.Add((Resolve-Path -LiteralPath $dest).Path)
    }

    if ($saved.Count -eq 0) {
        Exit-Fail '接口返回成功但没有图片数据（未找到 url 或 b64_json 字段）' 8
    }
    return $saved
}


function ConvertTo-AsciiJson {
    # 把 JSON 里的非 ASCII 字符转成 \uXXXX 转义。
    # 原因：PowerShell 7 的 ConvertTo-Json 不转义中文，输出到管道/重定向时按 GBK 编码，
    # 调用方（Agent、CI、其他程序）按 UTF-8 解码就会拿到乱码路径；纯 ASCII 则任何编码都能解。
    param([string]$JsonText)
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $JsonText.ToCharArray()) {
        if ([int]$ch -gt 127) { [void]$sb.AppendFormat('\u{0:x4}', [int]$ch) }
        else { [void]$sb.Append($ch) }
    }
    return $sb.ToString()
}


function Write-Result {
    param($Files, [string]$Model, [string]$Size, $Payload, [bool]$AsJson, [string]$Kind)

    if ($AsJson) {
        $obj = [ordered]@{
            ok       = $true
            files    = @($Files)
            model    = $Model
            size     = $Size
            cost_cny = $Payload.cost_cny
            trace_id = $Payload.trace_id
        }
        $jsonText = ConvertTo-Json -InputObject $obj -Depth 5 -Compress
        Write-Output (ConvertTo-AsciiJson -JsonText $jsonText)
        return
    }

    Write-Output "[OK] $Kind 成功（model=$Model size=$Size）"
    foreach ($f in $Files) { Write-Output "     $f" }
    $extras = @()
    if ($null -ne $Payload.cost_cny) { $extras += "费用 $($Payload.cost_cny) 元" }
    if ($Payload.trace_id) { $extras += "trace_id=$($Payload.trace_id)" }
    if ($extras.Count -gt 0) { Write-Output ('     ' + ($extras -join '  ')) }
    Write-Output '     图片已下载到本地（OSS 链接 24 小时过期），直接展示本地文件即可。'
}


# ---------------------------------------------------------------- 主流程
if ($Help -or [string]::IsNullOrWhiteSpace($Command)) {
    Show-Usage
    exit 0
}
if ($Command -notin @('gen', 'edit')) {
    Exit-Fail "未知命令 '$Command'，只支持 gen（文生图） / edit（垫图改图）" 2
}
if ([string]::IsNullOrWhiteSpace($ApiKey)) { $ApiKey = $ConfiguredApiKey }
if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    Exit-Fail @'
未配置 API Key。三选一：
  1) 打开本文件，把 $ConfiguredApiKey = '' 填成你的 Key
  2) 设置环境变量 TOKENRHYTHM_API_KEY
  3) 调用时加 -ApiKey sk_tr_xxx
'@ 2
}

if (-not [string]::IsNullOrWhiteSpace($PromptFile)) {
    if (-not (Test-Path -LiteralPath $PromptFile)) { Exit-Fail "提示词文件不存在：$PromptFile" 2 }
    $Prompt = (Get-Content -LiteralPath $PromptFile -Raw -Encoding UTF8).Trim()
}
if ([string]::IsNullOrWhiteSpace($Prompt)) {
    Exit-Fail '缺少提示词：用 -Prompt 直接传，或用 -PromptFile 指定 UTF-8 文本文件' 2
}

if ([string]::IsNullOrWhiteSpace($Out)) { $Out = (Get-Location).Path }
if (-not (Test-Path -LiteralPath $Out)) { New-Item -ItemType Directory -Path $Out -Force | Out-Null }
$OutDir = (Resolve-Path -LiteralPath $Out).Path
$BaseUrl = $BaseUrl.TrimEnd('/')
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

if ($Command -eq 'gen') {
    if ([string]::IsNullOrWhiteSpace($Size)) { $Size = '1024x1024' }
    $body = @{
        model           = $Model
        prompt          = $Prompt
        n               = $N
        size            = $Size
        response_format = 'url'
    }
    $resp = Invoke-TrApiJson -Url "$BaseUrl/v1/images/generations" -BodyObject $body -Key $ApiKey -TimeoutSec $TimeoutSec
    $files = Save-Images -Payload $resp -OutDir $OutDir -Prefix "tr-$stamp" -TimeoutSec $TimeoutSec
    Write-Result -Files $files -Model $Model -Size $Size -Payload $resp -AsJson:$Json -Kind '文生图'
}
else {
    if ([string]::IsNullOrWhiteSpace($Image)) { Exit-Fail 'edit 模式必须用 -Image 指定原图路径' 2 }
    if (-not (Test-Path -LiteralPath $Image)) { Exit-Fail "原图不存在：$Image" 2 }
    $imgItem = Get-Item -LiteralPath $Image
    if ($imgItem.Length -gt 10MB) {
        Exit-Fail ("原图 {0:N1}MB 超过 10MB 上限，请先压缩再试" -f ($imgItem.Length / 1MB)) 2
    }
    $fields = @{ model = $Model; prompt = $Prompt }
    if (-not [string]::IsNullOrWhiteSpace($Size)) { $fields['size'] = $Size }
    $resp = Invoke-TrApiMultipart -Url "$BaseUrl/v1/images/edits" -Fields $fields `
        -ImagePath $imgItem.FullName -Key $ApiKey -TimeoutSec $TimeoutSec
    $files = Save-Images -Payload $resp -OutDir $OutDir -Prefix "tr-edit-$stamp" -TimeoutSec $TimeoutSec
    Write-Result -Files $files -Model $Model -Size $Size -Payload $resp -AsJson:$Json -Kind '垫图改图'
}
