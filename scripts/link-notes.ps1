<#
.SYNOPSIS
  Show a project's private progress notes inside a code repository (native Windows),
  without git ever seeing them.

.DESCRIPTION
  link-notes.ps1 <ProjectFolder> <CodeRepo> [-Name progress]
  link-notes.ps1 -Unlink <CodeRepo> [-Name progress]

  ProjectFolder: the project's folder in your notes repo, relative to $env:ASSISTANT_NOTES
                 (e.g. work\website) or a full path.
  Creates <repo top>\progress pointing at that folder and adds "/progress" to the repo's
  .git\info\exclude (local only, never committed). Idempotent; refuses if "progress" is
  tracked by git or already exists as something else.

  Symlinks on Windows need Developer Mode (Settings > System > For developers) or an
  elevated shell. Without that, a directory JUNCTION is used instead (no special rights;
  same effect for editors and git, local disks only).
#>
param(
  [Parameter(Position = 0)][string]$ProjectFolder,
  [Parameter(Position = 1)][string]$CodeRepo,
  [switch]$Unlink,
  [string]$Name = "progress"
)
$ErrorActionPreference = "Stop"
if ($Name -notmatch '^[A-Za-z0-9._-]+$' -or $Name -in @('.', '..')) { throw "bad -Name: $Name" }
if ($Unlink) { $CodeRepo = if ($CodeRepo) { $CodeRepo } else { $ProjectFolder } }
if (-not $CodeRepo) { Get-Help $MyInvocation.MyCommand.Path -Detailed; exit 2 }

# ASSISTANT_NOTES from the environment or the kit's .env
$kit = Split-Path -Parent $PSScriptRoot
if (-not $env:ASSISTANT_NOTES -and (Test-Path "$kit\.env")) {
  $line = Get-Content "$kit\.env" | Where-Object { $_ -match '^\s*(export\s+)?ASSISTANT_NOTES=' } | Select-Object -Last 1
  if ($line) { $env:ASSISTANT_NOTES = ($line -replace '^\s*(export\s+)?ASSISTANT_NOTES=', '').Trim('"').Replace('~', $HOME).Replace('$HOME', $HOME) }
}

$top = (git -C $CodeRepo rev-parse --show-toplevel 2>$null)
if (-not $top) { throw "not a git repository: $CodeRepo" }
$top = $top.Trim() -replace '/', '\'
$link = Join-Path $top $Name
$exclude = (git -C $top rev-parse --git-path info/exclude).Trim()
if (-not [System.IO.Path]::IsPathRooted($exclude)) { $exclude = Join-Path $top $exclude }
$entry = "/$Name"

git -C $top ls-files --error-unmatch -- $Name *> $null
if ($LASTEXITCODE -eq 0 -or (git -C $top ls-files -- "$Name/")) { throw "refusing: '$Name' is tracked by git in $top" }

function Is-Link($p) { (Test-Path $p) -and ((Get-Item $p -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) }

if ($Unlink) {
  if (Is-Link $link) { (Get-Item $link -Force).Delete(); "removed link $link" }   # deletes the link only
  elseif (Test-Path $link) { throw "refusing: $link is not a link; left alone" }
  else { "no link at $link" }
  if (Test-Path $exclude) {
    $lines = Get-Content $exclude | Where-Object { $_ -ne $entry -and $_ -ne "# private progress notes (link-notes)" }
    Set-Content -Path $exclude -Value $lines
    "removed $entry from $exclude"
  }
  exit 0
}

if (Test-Path $ProjectFolder -PathType Container) { $target = (Resolve-Path $ProjectFolder).Path }
elseif ($env:ASSISTANT_NOTES -and (Test-Path (Join-Path $env:ASSISTANT_NOTES $ProjectFolder) -PathType Container)) {
  $target = (Resolve-Path (Join-Path $env:ASSISTANT_NOTES $ProjectFolder)).Path
} else { throw "notes folder not found: $ProjectFolder (relative to ASSISTANT_NOTES=$($env:ASSISTANT_NOTES), or a path)" }
if ($target.StartsWith($top + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "refusing: the notes folder is inside the code repo" }

if (Is-Link $link) {
  $cur = (Get-Item $link -Force).Target
  if ($cur -and ((Resolve-Path $cur).Path -eq $target)) { "link already in place: $link -> $target" }
  else { throw "refusing: $link already links to $cur; run -Unlink first" }
} elseif (Test-Path $link) { throw "refusing: $link exists and is not a link; move it first" }
else {
  try { New-Item -ItemType SymbolicLink -Path $link -Target $target | Out-Null; "linked $link -> $target (symlink)" }
  catch {
    New-Item -ItemType Junction -Path $link -Target $target | Out-Null
    "linked $link -> $target (junction: symlinks need Developer Mode or admin)"
  }
}

New-Item -ItemType Directory -Force -Path (Split-Path $exclude) | Out-Null
if (-not ((Test-Path $exclude) -and ((Get-Content $exclude) -contains $entry))) {
  Add-Content -Path $exclude -Value "`n# private progress notes (link-notes)`n$entry"
  "added $entry to $exclude (local only, never committed)"
}
$seen = git -C $top status --porcelain --untracked-files=all -- $Name
if ($seen) { throw "git still sees ${Name}: $seen" }
"verified: git status in $top does not show $Name\"
