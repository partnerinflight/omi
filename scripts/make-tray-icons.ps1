# Regenerates the tray/window icons from the Fluent UI "Brain Circuit" SVGs (MIT, see
# windows/SecondBrain.Tray/Assets/LICENSE-fluentui-system-icons.txt). Windows PowerShell 5.1+.
#   powershell -File scripts/make-tray-icons.ps1
# Writes brain.ico (plain), brain-attention.ico (amber dot) and brain-stopped.ico (red dot).
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationCore, WindowsBase
$assets = Join-Path $PSScriptRoot '..\windows\SecondBrain.Tray\Assets' | Resolve-Path
$sizes = 16, 20, 24, 32, 40, 48, 64, 256
$brain = [Windows.Media.Color]::FromRgb(0x8B, 0x5C, 0xF6)   # violet: legible on light and dark taskbars

function Get-Master([int]$px) {
    $svg = [xml](Get-Content (Join-Path $assets "brain-circuit-$px-filled.svg") -Raw)
    [pscustomobject]@{ Size = $px; Geometry = [Windows.Media.Geometry]::Parse($svg.svg.path.d) }
}
# The 20 px master is drawn for small sizes; the 48 px master keeps detail when larger.
$small = Get-Master 20; $large = Get-Master 48

function Render([int]$size, $dot) {
    $master = if ($size -le 24) { $small } else { $large }
    $visual = New-Object Windows.Media.DrawingVisual
    $dc = $visual.RenderOpen()
    $dc.PushTransform((New-Object Windows.Media.ScaleTransform ($size / $master.Size), ($size / $master.Size)))
    $dc.DrawGeometry((New-Object Windows.Media.SolidColorBrush $brain), $null, $master.Geometry)
    $dc.Pop()
    if ($dot) {
        # Status dot, bottom-right, with a ring cut out of the brain so it reads at 16 px.
        $r = [Math]::Max(3.0, $size * 0.22); $c = New-Object Windows.Point ($size - $r), ($size - $r)
        $dc.DrawEllipse([Windows.Media.Brushes]::White, $null, $c, $r, $r)
        $dc.DrawEllipse((New-Object Windows.Media.SolidColorBrush $dot), $null, $c, $r * 0.72, $r * 0.72)
    }
    $dc.Close()
    $bitmap = New-Object Windows.Media.Imaging.RenderTargetBitmap $size, $size, 96, 96, ([Windows.Media.PixelFormats]::Pbgra32)
    $bitmap.Render($visual)
    $pixels = New-Object byte[] ($size * $size * 4)
    $bitmap.CopyPixels($pixels, $size * 4, 0)
    for ($i = 0; $i -lt $pixels.Length; $i += 4) {   # icons use straight (not premultiplied) alpha
        $a = $pixels[$i + 3]
        if ($a -gt 0 -and $a -lt 255) { for ($k = 0; $k -lt 3; $k++) { $pixels[$i + $k] = [byte][Math]::Min(255, [Math]::Round($pixels[$i + $k] * 255.0 / $a)) } }
    }
    $pixels
}

function Write-Ico([string]$name, $dot) {
    $frames = foreach ($size in $sizes) {
        $pixels = Render $size $dot
        $mask = [int]([Math]::Ceiling($size / 32.0) * 4)          # AND mask row stride (all zero: alpha decides)
        $ms = New-Object IO.MemoryStream; $w = New-Object IO.BinaryWriter $ms
        $w.Write([int]40); $w.Write([int]$size); $w.Write([int]($size * 2)); $w.Write([int16]1); $w.Write([int16]32)
        $w.Write([int]0); $w.Write([int]($size * $size * 4 + $mask * $size)); $w.Write([int]0); $w.Write([int]0); $w.Write([int]0); $w.Write([int]0)
        for ($y = $size - 1; $y -ge 0; $y--) { $w.Write($pixels, $y * $size * 4, $size * 4) }   # bottom-up BGRA
        $w.Write((New-Object byte[] ($mask * $size)))
        $w.Flush(); [pscustomobject]@{ Size = $size; Data = $ms.ToArray() }
    }
    $out = New-Object IO.MemoryStream; $w = New-Object IO.BinaryWriter $out
    $w.Write([int16]0); $w.Write([int16]1); $w.Write([int16]$frames.Count)
    $offset = 6 + 16 * $frames.Count
    foreach ($f in $frames) {
        $dim = if ($f.Size -ge 256) { 0 } else { $f.Size }
        $w.Write([byte]$dim); $w.Write([byte]$dim); $w.Write([byte]0); $w.Write([byte]0)
        $w.Write([int16]1); $w.Write([int16]32); $w.Write([int]$f.Data.Length); $w.Write([int]$offset)
        $offset += $f.Data.Length
    }
    foreach ($f in $frames) { $w.Write($f.Data) }
    $w.Flush(); [IO.File]::WriteAllBytes((Join-Path $assets $name), $out.ToArray())
    Write-Host "wrote $name ($($out.Length) bytes, sizes $($sizes -join ', '))"
}

Write-Ico 'brain.ico' $null
Write-Ico 'brain-attention.ico' ([Windows.Media.Color]::FromRgb(0xF5, 0x9E, 0x0B))
Write-Ico 'brain-stopped.ico' ([Windows.Media.Color]::FromRgb(0xE8, 0x11, 0x23))
