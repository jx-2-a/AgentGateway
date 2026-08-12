# vpn_uia.ps1 - auto click Connect/Disconnect on Windows VPN settings page via UIAutomation
# usage: powershell -NoProfile -ExecutionPolicy Bypass -File vpn_uia.ps1 -Action connect|disconnect|check
# The VPN entry row (EntityItem, aid=SystemSettings_Connections_VPN_Collection_EntityItem)
# contains exactly one Button. Its AutomationId tells the state:
#   aid=DisconnectButton  -> connected state (Disconnect button)
#   aid=<empty>           -> disconnected state (Connect button)
# We locate the row by name and invoke its button.
# output protocol (ASCII, UTF-8 stdout):
#   INVOKED <Button>          clicked successfully
#   ALREADY_CONNECTED         already connected, nothing to do
#   ALREADY_DISCONNECTED      already disconnected, nothing to do
#   FAIL <reason>             failed; caller falls back to opening settings page
#   CHECK ...                 status report for check mode
param([Parameter(Mandatory=$true)][ValidateSet('connect','disconnect','check')][string]$Action)

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$VPN_NAME = 'USTC_VPN_FCK'
$script:RowBtn = $null
$script:RowBtnAid = ''

function Find-VpnFrame {
    Start-Process -FilePath cmd.exe -ArgumentList '/c start ms-settings:network-vpn' -WindowStyle Hidden
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $top = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
                   [System.Windows.Automation.Condition]::TrueCondition)
            foreach ($w in $top) {
                try {
                    if ($w.Current.ClassName -match 'ApplicationFrame') {
                        $all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                               [System.Windows.Automation.Condition]::TrueCondition)
                        foreach ($node in $all) {
                            if ($node.Current.Name -match $VPN_NAME) { return $w }
                        }
                    }
                } catch {}
            }
        } catch {}
    }
    return $null
}

function Find-RowButton($frame) {
    $rowCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
        'SystemSettings_Connections_VPN_Collection_EntityItem')
    $rows = $frame.FindAll([System.Windows.Automation.TreeScope]::Descendants, $rowCond)
    foreach ($r in $rows) {
        if ($r.Current.Name -notmatch $VPN_NAME) { continue }
        $kids = $r.FindAll([System.Windows.Automation.TreeScope]::Descendants,
               [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($k in $kids) {
            if ($k.Current.ControlType.ProgrammaticName -eq 'ControlType.Button') {
                $script:RowBtn = $k
                $script:RowBtnAid = $k.Current.AutomationId
                return
            }
        }
    }
}

function Test-Invokable($el) {
    if (-not $el) { return 'n/a' }
    $ok = $false
    try { $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern) | Out-Null; $ok = $true } catch {}
    if (-not $ok) {
        try { $el.GetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern) | Out-Null; $ok = $true } catch {}
    }
    return $(if ($ok) { 'yes' } else { 'no' })
}

function Invoke-Element($el) {
    if (-not $el) { return $false }
    try {
        $p = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $p.Invoke()
        return $true
    } catch {}
    try {
        $p = $el.GetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern)
        $p.DoDefaultAction()
        return $true
    } catch {}
    return $false
}

$frame = Find-VpnFrame
if (-not $frame) { Write-Output 'FAIL no_vpn_frame'; exit 1 }
Find-RowButton $frame

if (-not $script:RowBtn) {
    Write-Output 'FAIL row_button_not_found'
    exit 1
}
$connected = ($script:RowBtnAid -eq 'DisconnectButton')

if ($Action -eq 'check') {
    Write-Output ('CHECK row=found aid=' + $script:RowBtnAid +
        ' connected=' + $(if ($connected) { 'yes' } else { 'no' }) +
        ' invokable=' + (Test-Invokable $script:RowBtn))
    exit 0
}

if ($Action -eq 'connect') {
    if ($connected) { Write-Output 'ALREADY_CONNECTED'; exit 0 }
    if (Invoke-Element $script:RowBtn) { Write-Output 'INVOKED ConnectButton'; exit 0 }
    Write-Output 'FAIL invoke_connect_failed'
    exit 1
}

if ($Action -eq 'disconnect') {
    if (-not $connected) { Write-Output 'ALREADY_DISCONNECTED'; exit 0 }
    if (Invoke-Element $script:RowBtn) { Write-Output 'INVOKED DisconnectButton'; exit 0 }
    Write-Output 'FAIL invoke_disconnect_failed'
    exit 1
}

Write-Output 'FAIL unknown_action'
exit 1
