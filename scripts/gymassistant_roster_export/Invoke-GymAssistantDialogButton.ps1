param(
    [Parameter(Mandatory = $true)]
    [int]$ExpectedProcessId,
    [Parameter(Mandatory = $true)]
    [long]$WindowHandle,
    [Parameter(Mandatory = $true)]
    [AllowEmptyString()]
    [string]$ExpectedTitle,
    [Parameter(Mandatory = $true)]
    [string]$ButtonLabel
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version Latest

if ($env:OS -ne 'Windows_NT') {
    throw 'Deze UI Automation-helper werkt alleen op Windows.'
}
if ($ExpectedProcessId -le 0 -or $WindowHandle -le 0) {
    throw 'Ongeldig proces- of venster-ID.'
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Normalize-UiText {
    param([AllowEmptyString()][string]$Value)
    return (($Value -replace '&', '').Trim())
}

$window = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$WindowHandle)
if ($null -eq $window) {
    throw 'Het verwachte Gym Assistant-venster bestaat niet meer.'
}
if ([int]$window.Current.ProcessId -ne $ExpectedProcessId) {
    throw 'Het venster hoort niet bij het verwachte Gym Assistant-proces.'
}
if ([string]$window.Current.Name -cne $ExpectedTitle) {
    throw "Onverwachte venstertitel: '$($window.Current.Name)'."
}
if (-not $window.Current.IsEnabled) {
    throw 'Het verwachte Gym Assistant-venster is uitgeschakeld.'
}

$buttonCondition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Button
)
$buttons = $window.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    $buttonCondition
)
$wanted = Normalize-UiText $ButtonLabel
$matches = @()
$foundNames = @()
for ($index = 0; $index -lt $buttons.Count; $index++) {
    $button = $buttons.Item($index)
    $name = [string]$button.Current.Name
    $foundNames += $name
    if ((Normalize-UiText $name) -ceq $wanted) {
        $matches += $button
    }
}
if ($matches.Count -ne 1) {
    throw "Verwacht exact een knop '$ButtonLabel'; gevonden: $($matches.Count). Beschikbare knoppen: $($foundNames -join ', ')."
}

$selected = $matches[0]
if (-not $selected.Current.IsEnabled) {
    throw "De verwachte knop '$ButtonLabel' is uitgeschakeld."
}

$pattern = $null
if ($selected.TryGetCurrentPattern(
    [System.Windows.Automation.InvokePattern]::Pattern,
    [ref]$pattern
)) {
    ([System.Windows.Automation.InvokePattern]$pattern).Invoke()
    $method = 'InvokePattern'
} elseif ($selected.TryGetCurrentPattern(
    [System.Windows.Automation.LegacyIAccessiblePattern]::Pattern,
    [ref]$pattern
)) {
    ([System.Windows.Automation.LegacyIAccessiblePattern]$pattern).DoDefaultAction()
    $method = 'LegacyIAccessiblePattern'
} else {
    throw "De verwachte knop '$ButtonLabel' ondersteunt geen veilige UI Automation-actie."
}

[pscustomobject]@{
    status = 'invoked'
    process_id = $ExpectedProcessId
    window_handle = $WindowHandle
    window_title = $ExpectedTitle
    button = $ButtonLabel
    method = $method
} | ConvertTo-Json -Compress
