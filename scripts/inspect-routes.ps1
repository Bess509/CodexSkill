# Read-only IPv4 diagnostics. Never modifies routes, proxies, adapters or services.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]] $TargetHost
)

$ErrorActionPreference = 'Stop'
$diagnosticErrors = New-Object 'System.Collections.Generic.List[string]'
$activeRoutes = @()
$persistentRoutes = @()
$adapters = @()
$configurations = @()
$activeReadOk = $false
$persistentReadOk = $false

try {
    $activeRoutes = @(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore)
    $activeReadOk = $true
} catch { $diagnosticErrors.Add('ActiveStore: ' + $_.Exception.Message) }
try {
    $persistentRoutes = @(Get-NetRoute -AddressFamily IPv4 -PolicyStore PersistentStore)
    $persistentReadOk = $true
} catch { $diagnosticErrors.Add('PersistentStore: ' + $_.Exception.Message) }
try {
    $adapters = @(Get-NetAdapter -IncludeHidden | Select-Object Name, InterfaceIndex,
        Status, HardwareInterface, InterfaceDescription)
} catch { $diagnosticErrors.Add('Adapters: ' + $_.Exception.Message) }
try {
    $configurations = @(Get-NetIPConfiguration -All | ForEach-Object {
        [pscustomobject]@{
            InterfaceAlias = $_.InterfaceAlias
            InterfaceIndex = $_.InterfaceIndex
            IPv4Addresses = @($_.IPv4Address | ForEach-Object { $_.IPAddress })
            IPv4Gateways = @($_.IPv4DefaultGateway | ForEach-Object { $_.NextHop })
        }
    })
} catch { $diagnosticErrors.Add('IPConfiguration: ' + $_.Exception.Message) }

$targetResults = @(foreach ($hostValue in @($TargetHost | Sort-Object -Unique)) {
    $addresses = @()
    $resolutionError = $null
    $parsedAddress = $null
    try {
        if ([System.Net.IPAddress]::TryParse($hostValue, [ref] $parsedAddress)) {
            if ($parsedAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
                throw 'IPv6 input is outside this IPv4 diagnostic script.'
            }
            $addresses = @($parsedAddress.IPAddressToString)
        } else {
            if ($hostValue -match '[/\\:@\s]') {
                throw 'Provide a hostname or IPv4 address, without URL scheme, path, credentials or port.'
            }
            $addresses = @(Resolve-DnsName -Name $hostValue -Type A -DnsOnly |
                Where-Object { $_.Type -eq 'A' } |
                Select-Object -ExpandProperty IPAddress | Sort-Object -Unique)
            if ($addresses.Count -eq 0) { throw 'No A records returned.' }
        }
    } catch { $resolutionError = $_.Exception.Message }

    $routesForAddresses = @(foreach ($ipValue in $addresses) {
        $effectiveRoute = @()
        $routeError = $null
        try {
            $effectiveRoute = @(Find-NetRoute -RemoteIPAddress $ipValue |
                Select-Object DestinationPrefix, InterfaceIndex, InterfaceAlias,
                    NextHop, IPAddress, RouteMetric)
        } catch { $routeError = $_.Exception.Message }
        $hostPrefix = "$ipValue/32"
        [pscustomobject]@{
            IPv4 = $ipValue
            EffectiveSelection = $effectiveRoute
            SelectionError = $routeError
            ExactActive = @($activeRoutes | Where-Object DestinationPrefix -eq $hostPrefix |
                Select-Object DestinationPrefix, InterfaceIndex, InterfaceAlias, NextHop, RouteMetric)
            ExactPersistent = @($persistentRoutes | Where-Object DestinationPrefix -eq $hostPrefix |
                Select-Object DestinationPrefix, InterfaceIndex, InterfaceAlias, NextHop, RouteMetric)
        }
    })
    [pscustomobject]@{
        Host = $hostValue
        IPv4Addresses = $addresses
        ResolutionError = $resolutionError
        Routes = $routesForAddresses
    }
})

[pscustomobject]@{
    CapturedAt = (Get-Date).ToString('o')
    ReadOnly = $true
    IPv6Assessed = $false
    ActiveStoreReadSucceeded = $activeReadOk
    PersistentStoreReadSucceeded = $persistentReadOk
    Errors = @($diagnosticErrors.ToArray())
    Adapters = $adapters
    IPConfigurations = $configurations
    DefaultAndSplitDefaultRoutes = @($activeRoutes | Where-Object {
        $_.DestinationPrefix -in @('0.0.0.0/0', '0.0.0.0/1', '128.0.0.0/1')
    } | Select-Object DestinationPrefix, InterfaceIndex, InterfaceAlias, NextHop, RouteMetric)
    Targets = $targetResults
} | ConvertTo-Json -Depth 10
