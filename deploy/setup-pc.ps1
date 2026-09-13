<#
.SYNOPSIS
    Set up the canteen queue site on the PC that can see the camera.

.DESCRIPTION
    Run this on the cafeteria PC - the always-on machine with iVMS-4200.
    It finds the NVR, checks the credentials, writes both config files,
    generates the shared secrets, and optionally puts a public HTTPS URL in
    front of the site with a Cloudflare Tunnel.

    Nothing is installed without being asked for by a switch, and nothing is
    published without -Tunnel.

.EXAMPLE
    .\deploy\setup-pc.ps1 -NvrHost 10.0.12.40 -NvrUser queue

.EXAMPLE
    .\deploy\setup-pc.ps1 -NvrHost 10.0.12.40 -NvrUser queue -InstallFfmpeg -Tunnel -RegisterTasks
#>

[CmdletBinding()]
param(
    # NVR address. Omit to scan this PC's subnet for it.
    [string]$NvrHost,
    [string]$NvrUser = "queue",
    # Omit and you will be prompted, so the password stays out of your shell history.
    [string]$NvrPassword,
    [int]$IsapiPort = 80,
    # 0 = pick the first enabled sub-stream automatically.
    [int]$Channel = 0,
    [string]$CameraId = "cafeteria",
    [string]$CameraLabel = "Cafeteria queue",
    [string]$SiteName = "Canteen Queue",
    [int]$Port = 8080,
    # Access code for the live picture. Omit and one is generated for you.
    [string]$ViewCode,
    [switch]$InstallFfmpeg,
    [switch]$Tunnel,
    [switch]$RegisterTasks,
    [switch]$SkipCameraCheck
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Say($text)  { Write-Host "`n=== $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "  OK   $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  !!   $text" -ForegroundColor Yellow }
function Die($text)  { Write-Host "  FAIL $text" -ForegroundColor Red; exit 1 }

function Write-Utf8($path, $text) {
    # PowerShell 5.1's Set-Content -Encoding utf8 writes a BOM, which breaks
    # every JSON parser that opens the file as plain utf-8. Write it clean.
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

function New-Secret {
    # 32 random bytes, URL-safe. Good enough for a shared device key.
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ([Convert]::ToBase64String($bytes) -replace '[+/=]', '').Substring(0, 32)
}

# ---------------------------------------------------------------- python

Say "Checking Python"
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) {
    Die "Python not found. Install it from https://python.org (tick 'Add to PATH'), then re-run."
}
$version = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$version -lt [version]"3.11") {
    Die "Python $version found, but 3.11 or newer is needed."
}
Ok "Python $version at $python"

# ---------------------------------------------------------------- ffmpeg

Say "Checking ffmpeg"
$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if ($ffmpeg) {
    Ok "ffmpeg at $ffmpeg"
} elseif ($InstallFfmpeg) {
    Warn "installing ffmpeg via winget (this takes a minute)"
    winget install --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements
    $ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
    if (-not $ffmpeg) {
        Warn "ffmpeg installed but not on PATH in this shell - reopen the terminal afterwards"
    }
} else {
    Warn "ffmpeg not installed. That is fine: snapshot mode needs no ffmpeg at all."
    Warn "Re-run with -InstallFfmpeg later if you want smooth video instead of stills."
}

# ---------------------------------------------------------------- the NVR

$channelId = $Channel
$deviceLabel = $CameraLabel

if (-not $SkipCameraCheck) {
    Say "Finding the NVR"
    if (-not $NvrHost) {
        Warn "no -NvrHost given, scanning this subnet (unauthenticated, cannot lock anything out)"
        & $python "edge\hikvision.py" --scan
        Die "Pick the address above (or read it from iVMS-4200: Maintenance and Management -> Device Management) and re-run with -NvrHost."
    }

    if (-not $NvrPassword) {
        $secure = Read-Host "Password for NVR user '$NvrUser'" -AsSecureString
        $NvrPassword = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    }

    Say "Asking the NVR what it has"
    Warn "If the password is wrong, STOP. Hikvision locks out an IP after a few"
    Warn "failed logins, and that would cut this PC off from the school's CCTV."

    $raw = & $python "edge\hikvision.py" --host $NvrHost --port $IsapiPort `
        --user $NvrUser --password $NvrPassword --json
    $info = $raw | ConvertFrom-Json
    if (-not $info.ok) { Die $info.error }

    Ok "$($info.device.model) '$($info.device.name)' - firmware $($info.device.firmware)"
    Write-Host ""
    foreach ($c in $info.channels) {
        $name = $info.names."$($c.camera)"
        if (-not $name) { $name = $c.name }
        "    {0,4}  {1,-24} {2,-5} {3} {4}" -f $c.id, $name, $c.stream, $c.codec, $c.resolution | Write-Host
    }

    if ($channelId -eq 0) {
        # Sub-streams are the right default: a queue does not need 4K, and the
        # sub-stream is roughly a tenth of the upload and a tenth of the CPU.
        $pick = $info.channels | Where-Object { $_.stream -eq "sub" -and $_.enabled } | Select-Object -First 1
        if (-not $pick) { $pick = $info.channels | Select-Object -First 1 }
        if (-not $pick) { Die "the NVR reported no channels" }
        $channelId = $pick.id
        $named = $info.names."$($pick.camera)"
        if ($named) { $deviceLabel = $named }
        Write-Host ""
        Ok "using channel $channelId ($deviceLabel). Override with -Channel if that is the wrong camera."
    }

    if (-not $info.rtsp_reachable) {
        Warn "RTSP port 554 is not reachable - snapshot mode will work, smooth video will not."
    }
}

# ---------------------------------------------------------------- secrets

Say "Generating secrets"
$deviceKey = New-Secret
if (-not $ViewCode) { $ViewCode = "lunch-" + (New-Secret).Substring(0, 6).ToLower() }
Ok "device key generated (the pusher and server share it)"
Ok "access code for the live picture: $ViewCode"

$secretsPath = Join-Path $root "deploy\secrets.ps1"
@"
# Generated by setup-pc.ps1. Not in git - see .gitignore.
# The device key authenticates the pusher to the server.
# The access code gates the live picture; the queue count stays public.
`$env:DEVICE_KEY = "$deviceKey"
`$env:VIEW_CODE  = "$ViewCode"
"@ | ForEach-Object { Write-Utf8 $secretsPath $_ }
Ok "wrote deploy\secrets.ps1"

# ---------------------------------------------------------------- config

Say "Writing config"

$serverConfig = [ordered]@{
    site_name                  = $SiteName
    queue_name                 = $deviceLabel
    service_seconds_per_person = 11
    levels                     = @(
        [ordered]@{ name = "No queue"; max = 2;     tone = "clear" }
        [ordered]@{ name = "Short";    max = 7;     tone = "good"  }
        [ordered]@{ name = "Moderate"; max = 15;    tone = "warn"  }
        [ordered]@{ name = "Long";     max = 28;    tone = "bad"   }
        [ordered]@{ name = "Packed";   max = 10000; tone = "worst" }
    )
    stale_after_seconds        = 45
    offline_after_seconds      = 300
    snapshot_enabled           = $false
    open_hours                 = [ordered]@{ start = "07:30"; end = "16:00" }
    video_enabled              = $true
    video_note                 = "Live view is limited to staff and students with the access code."
    cameras                    = @([ordered]@{ id = $CameraId; label = $deviceLabel })
}
Write-Utf8 (Join-Path $root "server\config.json") ($serverConfig | ConvertTo-Json -Depth 6)
Ok "wrote server\config.json"

$rtsp = "rtsp://${NvrUser}:${NvrPassword}@${NvrHost}:554/Streaming/Channels/$channelId"
$pusherConfig = [ordered]@{
    source            = $rtsp
    server_url        = "http://127.0.0.1:$Port"
    device_key        = $deviceKey
    camera_id         = $CameraId
    camera_label      = $deviceLabel
    isapi             = [ordered]@{
        host     = $NvrHost
        port     = $IsapiPort
        user     = $NvrUser
        password = $NvrPassword
        channel  = $channelId
        interval = 1.5
    }
    mode              = "hls"
    transcode         = $false
    height            = 360
    bitrate           = "700k"
    segment_seconds   = 2
    snapshot_interval = 2.0
    snapshot_quality  = 6
    rtmp_url          = ""
    ffmpeg            = "ffmpeg"
}
Write-Utf8 (Join-Path $root "edge\pusher.json") ($pusherConfig | ConvertTo-Json -Depth 6)
Ok "wrote edge\pusher.json  (contains the NVR password - it is gitignored)"

# ---------------------------------------------------------------- launchers

Say "Writing launchers"

$source = "isapi"
if ($ffmpeg) { $source = $rtsp }

@"
# Starts the queue website. Generated by setup-pc.ps1.
Set-Location "$root"
. "$secretsPath"
python server\app.py --port $Port
"@ | ForEach-Object { Write-Utf8 (Join-Path $root "deploy\run-server.ps1") $_ }

@"
# Forwards the camera to the website. Generated by setup-pc.ps1.
Set-Location "$root"
. "$secretsPath"
python edge\pusher.py --camera $CameraId --source "$source"
"@ | ForEach-Object { Write-Utf8 (Join-Path $root "deploy\run-pusher.ps1") $_ }

@"
# Publishes the queue count (not the picture). Generated by setup-pc.ps1.
Set-Location "$root"
. "$secretsPath"
python edge\counter.py
"@ | ForEach-Object { Write-Utf8 (Join-Path $root "deploy\run-counter.ps1") $_ }

Ok "wrote deploy\run-server.ps1, run-pusher.ps1, run-counter.ps1"
if ($source -eq "isapi") {
    Warn "pusher set to snapshot mode (no ffmpeg). Install ffmpeg and re-run for smooth video."
}

# ---------------------------------------------------------------- tasks

if ($RegisterTasks) {
    Say "Registering scheduled tasks"
    foreach ($pair in @(@("QueueServer", "run-server.ps1"), @("QueuePusher", "run-pusher.ps1"))) {
        $name = $pair[0]
        $script = Join-Path $root "deploy\$($pair[1])"
        $action = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
            -Settings $settings -RunLevel Highest -Force | Out-Null
        Ok "$name registered (starts at boot, restarts on failure)"
    }
}

# ---------------------------------------------------------------- tunnel

if ($Tunnel) {
    Say "Public URL via Cloudflare Tunnel"
    Warn "This publishes the site on the public internet."
    Warn "The queue count will be readable by anyone with the link."
    Warn "The live picture stays behind the access code: $ViewCode"

    $cloudflared = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
    if (-not $cloudflared) {
        Warn "installing cloudflared via winget"
        winget install --id Cloudflare.cloudflared --silent --accept-package-agreements --accept-source-agreements
        $cloudflared = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
    }
    if (-not $cloudflared) {
        Warn "cloudflared not on PATH in this shell. Reopen the terminal and run:"
        Warn "  cloudflared tunnel --url http://localhost:$Port"
    } else {
        @"
# Public HTTPS URL for the site. Generated by setup-pc.ps1.
cloudflared tunnel --url http://localhost:$Port
"@ | ForEach-Object { Write-Utf8 (Join-Path $root "deploy\run-tunnel.ps1") $_ }
        Ok "wrote deploy\run-tunnel.ps1"
    }
}

# ---------------------------------------------------------------- done

Say "Done"
Write-Host @"

Start it, each in its own terminal:

    .\deploy\run-server.ps1      the website        http://localhost:$Port
    .\deploy\run-pusher.ps1      the camera feed
    .\deploy\run-counter.ps1     the queue count    (optional, needs: pip install -r edge\requirements.txt)

Access code for the live picture:  $ViewCode

Check it works:  open http://localhost:$Port on this PC.
Then, for a public URL:  .\deploy\run-tunnel.ps1

Before publishing, read docs\deploy.md - it covers who should be able to see
this and what to put in writing first.
"@ -ForegroundColor White
