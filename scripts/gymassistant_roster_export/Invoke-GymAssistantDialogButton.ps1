param(
    [Parameter(Mandatory = $true)]
    [int]$ExpectedProcessId,
    [Parameter(Mandatory = $true)]
    [long]$WindowHandle,
    [Parameter(Mandatory = $true)]
    [long]$ButtonHandle,
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
    throw 'Deze Windows-toegankelijkheidshelper werkt alleen op Windows.'
}
if ($ExpectedProcessId -le 0 -or $WindowHandle -le 0 -or $ButtonHandle -le 0) {
    throw 'Ongeldig proces-, venster- of knop-ID.'
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

namespace Dreamz.GymAssistantRosterExport
{
    public static class NativeWindow
    {
        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool IsWindow(IntPtr hWnd);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool IsChild(IntPtr hWndParent, IntPtr hWnd);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool IsWindowEnabled(IntPtr hWnd);

        [DllImport("user32.dll")]
        public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetWindowTextLengthW(IntPtr hWnd);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int maxCount);

        [DllImport("oleacc.dll")]
        public static extern int AccessibleObjectFromWindow(
            IntPtr hWnd,
            uint objectId,
            ref Guid interfaceId,
            [MarshalAs(UnmanagedType.Interface)] out object accessible
        );

        public static string GetText(IntPtr hWnd)
        {
            int length = GetWindowTextLengthW(hWnd);
            var text = new StringBuilder(length + 1);
            GetWindowTextW(hWnd, text, text.Capacity);
            return text.ToString();
        }
    }
}
'@

function Normalize-UiText {
    param([AllowEmptyString()][string]$Value)
    return (($Value -replace '&', '').Trim())
}

$windowPointer = [IntPtr]$WindowHandle
$buttonPointer = [IntPtr]$ButtonHandle
if (-not [Dreamz.GymAssistantRosterExport.NativeWindow]::IsWindow($windowPointer)) {
    throw 'Het verwachte Gym Assistant-venster bestaat niet meer.'
}
if (-not [Dreamz.GymAssistantRosterExport.NativeWindow]::IsWindow($buttonPointer)) {
    throw 'De verwachte Gym Assistant-knop bestaat niet meer.'
}

[uint32]$windowProcessId = 0
[uint32]$buttonProcessId = 0
[void][Dreamz.GymAssistantRosterExport.NativeWindow]::GetWindowThreadProcessId(
    $windowPointer,
    [ref]$windowProcessId
)
[void][Dreamz.GymAssistantRosterExport.NativeWindow]::GetWindowThreadProcessId(
    $buttonPointer,
    [ref]$buttonProcessId
)
if ($windowProcessId -ne $ExpectedProcessId -or $buttonProcessId -ne $ExpectedProcessId) {
    throw 'Het venster of de knop hoort niet bij het verwachte Gym Assistant-proces.'
}
if (-not [Dreamz.GymAssistantRosterExport.NativeWindow]::IsChild(
    $windowPointer,
    $buttonPointer
)) {
    throw 'De verwachte knop hoort niet bij het verwachte Gym Assistant-venster.'
}

$discoveredTitle = [Dreamz.GymAssistantRosterExport.NativeWindow]::GetText($windowPointer)
if ($discoveredTitle -cne $ExpectedTitle) {
    throw "Onverwacht venster: '$discoveredTitle'; verwacht '$ExpectedTitle'."
}
if (-not [Dreamz.GymAssistantRosterExport.NativeWindow]::IsWindowEnabled($windowPointer)) {
    throw 'Het verwachte Gym Assistant-venster is uitgeschakeld.'
}

$wanted = Normalize-UiText $ButtonLabel
$nativeButtonText = [Dreamz.GymAssistantRosterExport.NativeWindow]::GetText($buttonPointer)
if ((Normalize-UiText $nativeButtonText) -cne $wanted) {
    throw "Onverwacht native knoplabel: '$nativeButtonText'; verwacht '$ButtonLabel'."
}
if (-not [Dreamz.GymAssistantRosterExport.NativeWindow]::IsWindowEnabled($buttonPointer)) {
    throw "De verwachte knop '$ButtonLabel' is uitgeschakeld."
}

$accessibleInterfaceId = [Guid]'618736E0-3C3D-11CF-810C-00AA00389B71'
$objectIdClient = [uint32]4294967292
$roleSystemPushButton = 43
$stateSystemUnavailable = 1
$accessible = $null
$resultCode = [Dreamz.GymAssistantRosterExport.NativeWindow]::AccessibleObjectFromWindow(
    $buttonPointer,
    $objectIdClient,
    [ref]$accessibleInterfaceId,
    [ref]$accessible
)
if ($resultCode -lt 0 -or $null -eq $accessible) {
    throw "Windows kon de verwachte knop niet via Microsoft Active Accessibility openen (HRESULT $resultCode)."
}

try {
    $accessibleName = [string]$accessible.accName(0)
    $accessibleRole = [int]$accessible.accRole(0)
    $accessibleState = [int]$accessible.accState(0)
    $defaultAction = [string]$accessible.accDefaultAction(0)

    if ((Normalize-UiText $accessibleName) -cne $wanted) {
        throw "Onverwacht toegankelijkheidslabel: '$accessibleName'; verwacht '$ButtonLabel'."
    }
    if ($accessibleRole -ne $roleSystemPushButton) {
        throw "Het verwachte toegankelijke element is geen drukknop (rol $accessibleRole)."
    }
    if (($accessibleState -band $stateSystemUnavailable) -ne 0) {
        throw "De verwachte knop '$ButtonLabel' is volgens Windows niet beschikbaar."
    }
    if ([string]::IsNullOrWhiteSpace($defaultAction)) {
        throw "De verwachte knop '$ButtonLabel' heeft geen standaardactie."
    }

    $accessible.accDoDefaultAction(0)
} finally {
    if ($null -ne $accessible -and [Runtime.InteropServices.Marshal]::IsComObject($accessible)) {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($accessible)
    }
}

[pscustomobject]@{
    status = 'invoked'
    process_id = $ExpectedProcessId
    window_handle = $WindowHandle
    button_handle = $ButtonHandle
    window_title = $ExpectedTitle
    button = $ButtonLabel
    discovered_button_name = $accessibleName
    accessible_role = $accessibleRole
    accessible_state = $accessibleState
    default_action = $defaultAction
    method = 'MSAA.accDoDefaultAction'
} | ConvertTo-Json -Compress
