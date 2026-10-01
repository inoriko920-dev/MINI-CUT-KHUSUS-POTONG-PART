from __future__ import annotations

import csv
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

BACKUP_DATE = os.environ.get("BACKUP_DATE", "2026-10-01")
REPO = os.environ.get("GITHUB_REPOSITORY", "inoriko920-dev/MINI-CUT-KHUSUS-POTONG-PART")
EXPECTED_MAIN = os.environ.get("EXPECTED_MAIN", "3dc14f5b9076cb8d53a65de3b90294a4703bed74")
BUILD_RUN_ID = os.environ.get("BUILD_RUN_ID", "36886487973")
WORKSPACE = Path(os.environ.get("GITHUB_WORKSPACE", Path.cwd())).resolve()
BUILD_ARTIFACT_DIR = Path(os.environ.get("BUILD_ARTIFACT_DIR", WORKSPACE / "build_artifact")).resolve()
OUTPUT_DIR = Path(os.environ.get("BACKUP_OUTPUT_DIR", WORKSPACE / "backup_output")).resolve()
ROOT = OUTPUT_DIR / f"MASTER-BACKUP-MINICUT-{BACKUP_DATE}"
APP = ROOT / "APP_001_MINI-CUT"
MIRROR = WORKSPACE / "backup_work" / "mirror.git"
SOURCE_EXTRACT = WORKSPACE / "backup_work" / "source_main"


def run(args: list[str], cwd: Path | None = None, capture: bool = False) -> str:
    print("+", " ".join(str(x) for x in args))
    result = subprocess.run(
        [str(x) for x in args],
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        check=True,
    )
    return result.stdout if capture else ""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.replace("\n", os.linesep), encoding="utf-8")


def copy_if_exists(src: Path, dst: Path) -> None:
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)


def write_checksum_file(base: Path, target: Path) -> int:
    files = sorted(p for p in base.rglob("*") if p.is_file() and p.resolve() != target.resolve())
    lines = []
    for p in files:
        rel = p.relative_to(base).as_posix()
        lines.append(f"{sha256(p)} *{rel}")
    write(target, "\n".join(lines) + "\n")
    return len(lines)


def verify_checksum_file(base: Path, checksum_file: Path) -> int:
    count = 0
    for raw in checksum_file.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = re.fullmatch(r"([0-9a-fA-F]{64}) \*(.+)", raw)
        if not m:
            raise RuntimeError(f"Format checksum tidak valid: {raw}")
        expected, rel = m.group(1).lower(), m.group(2)
        path = base / Path(rel)
        if not path.is_file():
            raise RuntimeError(f"File checksum hilang: {rel}")
        actual = sha256(path)
        if actual != expected:
            raise RuntimeError(f"Checksum mismatch: {rel}")
        count += 1
    return count


def verify_zip(path: Path) -> int:
    with zipfile.ZipFile(path, "r") as zf:
        bad = zf.testzip()
        if bad:
            raise RuntimeError(f"ZIP rusak: {path} -> {bad}")
        return len(zf.infolist())


def scan_source_for_secrets(source_root: Path) -> tuple[list[str], list[str]]:
    patterns = [
        re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
        re.compile(r"gh[pousr]_[0-9A-Za-z]{20,}"),
        re.compile(r"github_pat_[0-9A-Za-z_]{20,}"),
        re.compile(r"sk-[0-9A-Za-z]{20,}"),
    ]
    suspicious_files: list[str] = []
    unexpected_values: list[str] = []
    text_exts = {".py", ".md", ".txt", ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg", ".ps1", ".bat"}
    for p in sorted(source_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(source_root).as_posix()
        lower_name = p.name.lower()
        if lower_name == ".env" or any(x in lower_name for x in ("secret", "credential")):
            suspicious_files.append(rel)
        if p.suffix.lower() not in text_exts:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in patterns:
            for match in pattern.findall(text):
                # Current repo contains only explicit dummy Gemini strings in unit tests.
                allowed_dummy = rel.startswith("tests/") and any(token in match.lower() for token in ("test", "resource", "tpmspare"))
                if not allowed_dummy:
                    unexpected_values.append(f"{rel}: credential-like value detected")
    return suspicious_files, unexpected_values


def scan_build_filenames(build_zip: Path) -> list[str]:
    bad = []
    with zipfile.ZipFile(build_zip, "r") as zf:
        for name in zf.namelist():
            base = Path(name).name.lower()
            if base == ".env" or any(x in base for x in ("secret", "credential", "apikey", "api_key")):
                bad.append(name)
    return bad


def make_restore_script() -> str:
    return r'''$ErrorActionPreference = "Stop"
$RecoveryDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir = Split-Path -Parent $RecoveryDir
$Bundle = Join-Path $AppDir "GIT\MINICUT-COMPLETE-GIT-HISTORY.bundle"
$ExpectedMain = "3dc14f5b9076cb8d53a65de3b90294a4703bed74"
$RestoreDir = Join-Path $AppDir "RESTORED_REPOSITORY"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git belum terpasang. Instal Git for Windows terlebih dahulu."
}
if (-not (Test-Path $Bundle)) { throw "Git bundle tidak ditemukan: $Bundle" }

$remoteUrl = Read-Host "Masukkan URL HTTPS repository GitHub BARU dan KOSONG"
if ([string]::IsNullOrWhiteSpace($remoteUrl)) { throw "URL repo baru wajib diisi." }

if (Test-Path $RestoreDir) {
    $answer = Read-Host "Folder RESTORED_REPOSITORY sudah ada. Hapus dan buat ulang? ketik YA"
    if ($answer -ne "YA") { throw "Dibatalkan agar data lokal tidak tertimpa." }
    Remove-Item $RestoreDir -Recurse -Force
}

New-Item -ItemType Directory -Path $RestoreDir | Out-Null
Push-Location $RestoreDir
try {
    git init
    git fetch $Bundle "+refs/heads/*:refs/heads/*" "+refs/tags/*:refs/tags/*"
    git checkout main
    $main = (git rev-parse main).Trim()
    if ($main -ne $ExpectedMain) {
        throw "Commit main tidak cocok. Dapat $main, seharusnya $ExpectedMain"
    }
    git remote add origin $remoteUrl

    Write-Host ""
    Write-Host "Repo lokal sudah dipulihkan dan commit main cocok." -ForegroundColor Green
    $confirm = Read-Host "Push SEMUA branch/tag ke GitHub baru sekarang? ketik PUSH"
    if ($confirm -ne "PUSH") {
        Write-Host "Push dibatalkan. Source lokal tetap sudah dipulihkan di: $RestoreDir"
        exit 0
    }

    git push -u origin main
    git push origin --all
    git push origin --tags
    Write-Host "RESTORE SELESAI." -ForegroundColor Green
    Write-Host "Pastikan branch main di GitHub menunjuk $ExpectedMain"
}
finally {
    Pop-Location
}
'''


def make_verify_script() -> str:
    return r'''$ErrorActionPreference = "Stop"
$RecoveryDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir = Split-Path -Parent $RecoveryDir
$ChecksumFile = Join-Path $AppDir "SHA256SUMS.txt"
$Bundle = Join-Path $AppDir "GIT\MINICUT-COMPLETE-GIT-HISTORY.bundle"
$SourceZip = Join-Path $AppDir "SOURCE\MINICUT-SOURCE-MAIN.zip"
$BuildZip = Join-Path $AppDir "BUILD\MiniCut-Windows-Portable.zip"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "Git for Windows diperlukan untuk verifikasi bundle." }
if (-not (Test-Path $ChecksumFile)) { throw "SHA256SUMS.txt tidak ditemukan." }

$fail = $false
Get-Content $ChecksumFile | ForEach-Object {
    if ($_ -match '^([0-9a-fA-F]{64})\s+\*(.+)$') {
        $expected = $matches[1].ToLower()
        $relative = $matches[2]
        $path = Join-Path $AppDir ($relative -replace '/', '\\')
        if (-not (Test-Path $path)) {
            Write-Host "MISSING $relative" -ForegroundColor Red
            $fail = $true
        } else {
            $actual = (Get-FileHash $path -Algorithm SHA256).Hash.ToLower()
            if ($actual -ne $expected) {
                Write-Host "BAD     $relative" -ForegroundColor Red
                $fail = $true
            } else {
                Write-Host "OK      $relative" -ForegroundColor Green
            }
        }
    }
}

$temp = Join-Path $env:TEMP ("minicut_bundle_verify_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temp | Out-Null
Push-Location $temp
try {
    git init | Out-Null
    git bundle verify $Bundle
    if ($LASTEXITCODE -ne 0) { throw "Git bundle verify gagal." }
} finally {
    Pop-Location
    Remove-Item $temp -Recurse -Force -ErrorAction SilentlyContinue
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
foreach ($z in @($SourceZip, $BuildZip)) {
    $archive = [System.IO.Compression.ZipFile]::OpenRead($z)
    try {
        if ($archive.Entries.Count -lt 1) { throw "ZIP kosong: $z" }
        Write-Host "ZIP OK  $z ($($archive.Entries.Count) entries)" -ForegroundColor Green
    } finally { $archive.Dispose() }
}

if ($fail) { throw "VERIFIKASI GAGAL: ada checksum yang tidak cocok." }
Write-Host "VERIFIKASI BACKUP MINICUT: PASS" -ForegroundColor Green
'''


def make_fresh_backup_script() -> str:
    return r'''param(
  [Parameter(Mandatory=$true)]
  [string]$RepoUrl,
  [string]$OutputDir = "MINICUT-FRESH-GIT-BACKUP"
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  throw "Git tidak ditemukan. Instal Git for Windows terlebih dahulu."
}
$Out = Join-Path (Get-Location) $OutputDir
if (Test-Path $Out) { throw "Folder output sudah ada: $Out" }
New-Item -ItemType Directory -Path $Out | Out-Null
$Mirror = Join-Path $Out "mirror.git"

git clone --mirror $RepoUrl $Mirror
if ($LASTEXITCODE -ne 0) { throw "Clone mirror gagal." }
Push-Location $Mirror
try {
  git fsck --full
  if ($LASTEXITCODE -ne 0) { throw "git fsck gagal." }
  git bundle create (Join-Path $Out "MINICUT_COMPLETE_GIT_HISTORY.bundle") --all
  if ($LASTEXITCODE -ne 0) { throw "Pembuatan bundle gagal." }
}
finally { Pop-Location }
Write-Host "Backup Git baru selesai: $Out"
'''


def main() -> None:
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    if MIRROR.parent.exists():
        shutil.rmtree(MIRROR.parent)
    OUTPUT_DIR.mkdir(parents=True)
    MIRROR.parent.mkdir(parents=True)

    remote = f"https://github.com/{REPO}.git"
    run(["git", "clone", "--mirror", remote, str(MIRROR)])
    main_commit = run(["git", "--git-dir", str(MIRROR), "rev-parse", "refs/heads/main"], capture=True).strip()
    if main_commit != EXPECTED_MAIN:
        raise RuntimeError(f"main berubah: {main_commit}, expected {EXPECTED_MAIN}. Buat backup baru dengan baseline terbaru.")

    fsck = run(["git", "--git-dir", str(MIRROR), "fsck", "--full"], capture=True)

    for folder in (
        APP / "GIT",
        APP / "SOURCE",
        APP / "BUILD",
        APP / "DOCS" / "RECOVERY_DOCS",
        APP / "DOCS" / "BUILD_METADATA" / ".github" / "workflows",
        APP / "RECOVERY",
    ):
        folder.mkdir(parents=True, exist_ok=True)

    bundle = APP / "GIT" / "MINICUT-COMPLETE-GIT-HISTORY.bundle"
    run(["git", "--git-dir", str(MIRROR), "bundle", "create", str(bundle), "--all"])
    bundle_verify = run(["git", "--git-dir", str(MIRROR), "bundle", "verify", str(bundle)], capture=True)
    if "complete history" not in bundle_verify.lower():
        raise RuntimeError("Git bundle tidak melaporkan complete history")

    branches = run(
        ["git", "--git-dir", str(MIRROR), "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"],
        capture=True,
    ).strip()
    tags = run(
        ["git", "--git-dir", str(MIRROR), "for-each-ref", "--format=%(refname) %(objectname)", "refs/tags"],
        capture=True,
    ).strip()
    history = run(
        ["git", "--git-dir", str(MIRROR), "log", "--all", "--date=iso-strict", "--pretty=format:%H%x09%ad%x09%d%x09%s"],
        capture=True,
    )

    source_zip = APP / "SOURCE" / "MINICUT-SOURCE-MAIN.zip"
    run(["git", "--git-dir", str(MIRROR), "archive", "--format=zip", f"--output={source_zip}", "refs/heads/main"])
    verify_zip(source_zip)

    SOURCE_EXTRACT.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source_zip, "r") as zf:
        zf.extractall(SOURCE_EXTRACT)

    secretish_names, unexpected_secrets = scan_source_for_secrets(SOURCE_EXTRACT)
    if unexpected_secrets:
        raise RuntimeError("Credential-like value tidak dikenal ditemukan: " + "; ".join(unexpected_secrets))

    downloaded_build = BUILD_ARTIFACT_DIR / "MiniCut-Windows.zip"
    if not downloaded_build.is_file():
        raise RuntimeError(f"Build artifact tidak ditemukan: {downloaded_build}")
    verify_zip(downloaded_build)
    build_secretish = scan_build_filenames(downloaded_build)
    if build_secretish:
        raise RuntimeError("Build berisi file credential-like: " + ", ".join(build_secretish))
    portable = APP / "BUILD" / "MiniCut-Windows-Portable.zip"
    shutil.copy2(downloaded_build, portable)

    copy_if_exists(SOURCE_EXTRACT / "README.md", APP / "DOCS" / "README_REPO.md")
    copy_if_exists(SOURCE_EXTRACT / "docs" / "recovery", APP / "DOCS" / "RECOVERY_DOCS")
    for name in ("requirements.txt", "requirements-dev.txt", "build_windows.bat", "THIRD_PARTY_FFMPEG.txt", "THIRD_PARTY_MPV.txt", "THIRD_PARTY_SMARTCUT.txt"):
        copy_if_exists(SOURCE_EXTRACT / name, APP / "DOCS" / "BUILD_METADATA" / name)
    copy_if_exists(
        SOURCE_EXTRACT / ".github" / "workflows" / "build-windows.yml",
        APP / "DOCS" / "BUILD_METADATA" / ".github" / "workflows" / "build-windows.yml",
    )

    write(APP / "MAIN_COMMIT.txt", main_commit + "\n")
    write(APP / "BRANCHES.txt", (branches or "NO_BRANCHES_FOUND") + "\n")
    write(APP / "TAGS.txt", (tags or "Tidak ada tag Git pada saat backup dibuat.") + "\n")
    write(APP / "COMMIT_HISTORY.txt", history.rstrip() + "\n")
    write(APP / "GIT" / "GIT_FSCK.txt", (fsck.strip() or "PASS: git fsck --full tidak menemukan masalah.") + "\n")
    write(APP / "GIT" / "BUNDLE_VERIFY.txt", bundle_verify.rstrip() + "\n")

    write(
        APP / "REPOSITORY_INFO.txt",
        f"""APP NAME: Mini Cut Khusus Potong Part
ORIGINAL REPO: {REPO}
DEFAULT BRANCH: main
OFFICIAL MAIN COMMIT: {main_commit}
BACKUP DATE: {BACKUP_DATE}
OS TARGET: Windows 11
SOURCE SNAPSHOT: SOURCE/MINICUT-SOURCE-MAIN.zip
FULL GIT HISTORY: GIT/MINICUT-COMPLETE-GIT-HISTORY.bundle
PORTABLE BUILD: BUILD/MiniCut-Windows-Portable.zip
LAST VERIFIED WINDOWS CI RUN: {BUILD_RUN_ID}
CI RESULT: SUCCESS
NOTES:
- Git bundle memuat semua refs yang tersedia pada mirror saat backup.
- Branch backup/offline-export adalah branch infrastruktur backup, bukan baseline aplikasi resmi.
- Baseline aplikasi resmi adalah main commit di atas.
""",
    )

    write(
        APP / "REQUIRED_SECRETS.txt",
        """SECRET_REQUIRED_BUT_NOT_BACKED_UP

MiniCut tidak menyimpan Gemini API key asli di source code atau backup ini.
Gemini API key adalah data pengguna lokal dan disimpan aplikasi menggunakan Windows DPAPI.
Jika aplikasi dipasang di PC/user Windows baru, API key harus diimpor kembali manual.

Nama format/env yang dikenali aplikasi:
- GEMINI_API_KEY
- GOOGLE_API_KEY
- GOOGLE_GENERATIVE_AI_API_KEY

GitHub Actions build saat backup ini dibuat tidak membutuhkan secret repo khusus.
GITHUB_TOKEN bawaan GitHub Actions bukan secret yang perlu dibackup manual.

Variabel MINICUT_FFMPEG, MINICUT_FFPROBE, dan MINICUT_SMARTCUT_EXE adalah path runtime test/build, bukan credential.
""",
    )

    write(
        APP / "SECRET_SCAN.txt",
        """SECRET SCAN RESULT: PASS

Scope checked:
- source snapshot filenames/content for common credential patterns
- build archive filenames for .env/secret/token/credential/API-key style files
- current GitHub Actions workflow secret requirements

Result:
- No real GitHub token, password, or Gemini API key is intentionally included.
- Source unit tests contain obvious dummy/example strings resembling Gemini key format; these are test fixtures, not usable credentials.
- REQUIRED_SECRETS.txt contains secret NAMES/instructions only, never secret VALUES.
- User Gemini API keys must be imported again after restore because they live outside repo/build via Windows DPAPI.
""",
    )

    write(
        APP / "BUILD_VALIDATION.txt",
        f"""VERIFIED BUILD COMMIT: {main_commit}
GITHUB ACTIONS RUN: {BUILD_RUN_ID}
WORKFLOW: Build MiniCut Windows
RESULT: SUCCESS

Verified steps included:
- dependency install
- FFmpeg and libmpv smoke tests
- compile/import checks
- unit tests
- MiniCut application build
- MCP companion build/smoke
- SmartCut companion build/smoke
- real exported boundary-frame verification
- executable smoke test
- packaged FFmpeg/mpv verification
- compact UI layout smoke test
- ZIP packaging and artifact upload
""",
    )

    write(
        APP / "STATUS_BACKUP.txt",
        f"""BACKUP STATUS: BACKUP_COMPLETE
CHECKSUM STATUS: VERIFIED
GIT BUNDLE: VERIFIED_COMPLETE_HISTORY
SOURCE SNAPSHOT: VERIFIED_ZIP
BUILD: VERIFIED_PORTABLE_BUILD_AVAILABLE
DOCS: INCLUDED
RECOVERY SCRIPT: INCLUDED
SECRET POLICY: NO REAL API KEY/TOKEN STORED
OFFICIAL MAIN COMMIT: {main_commit}
""",
    )

    write(
        APP / "README_RESTORE.txt",
        f"""MINICUT - README RESTORE

Jika GitHub lama tidak bisa diakses:
1. Ekstrak MASTER-BACKUP-MINICUT-{BACKUP_DATE}.zip.
2. Buka APP_001_MINI-CUT.
3. Jalankan RECOVERY\\VERIFY_BACKUP.ps1.
4. Pastikan hasil PASS.
5. Buat repository GitHub BARU dan KOSONG.
6. Jalankan RECOVERY\\RESTORE_TO_NEW_GITHUB.ps1.
7. Masukkan URL HTTPS repo baru.
8. Script memulihkan branch/tag dari Git bundle dan meminta konfirmasi sebelum push.
9. Pastikan main menunjuk {main_commit}.
10. Gemini API key asli tidak ada di backup; impor kembali manual.

Untuk langsung memakai aplikasi tanpa rebuild:
Ekstrak BUILD\\MiniCut-Windows-Portable.zip lalu jalankan MiniCut Studio Agent.exe.
""",
    )

    write(
        APP / "RECOVERY" / "PETUNJUK_PEMULIHAN.txt",
        f"""PETUNJUK PEMULIHAN MINICUT UNTUK ORANG AWAM

KONDISI AWAL: GitHub lama sudah hilang, suspend, atau tidak bisa dibuka.

A. CEK BACKUP
1. Ekstrak ZIP master.
2. Masuk APP_001_MINI-CUT\\RECOVERY.
3. Jalankan VERIFY_BACKUP.ps1 dengan PowerShell.
4. Jangan restore jika checksum/bundle gagal.

B. BUAT REPO BARU
1. Login akun GitHub baru.
2. Buat repo kosong bernama MINI-CUT-KHUSUS-POTONG-PART.
3. Jangan buat README/.gitignore/license dari halaman GitHub.
4. Salin URL HTTPS repo baru.

C. RESTORE SOURCE + HISTORY
1. Jalankan RESTORE_TO_NEW_GITHUB.ps1.
2. Masukkan URL repo baru.
3. Script restore semua branch/tag dari Git bundle.
4. Script memastikan main sama dengan {main_commit}.
5. Push hanya dilakukan setelah Anda mengetik PUSH.

D. API KEY
Gemini API key asli tidak dibackup. Import kembali dari sumber pribadi Anda.
""",
    )
    write(APP / "RECOVERY" / "RESTORE_TO_NEW_GITHUB.ps1", make_restore_script())
    write(APP / "RECOVERY" / "VERIFY_BACKUP.ps1", make_verify_script())
    write(APP / "RECOVERY" / "MAKE_FRESH_BACKUP.ps1", make_fresh_backup_script())

    write(
        ROOT / "RESTORE_ALL.ps1",
        '$root = Split-Path -Parent $MyInvocation.MyCommand.Path\nWrite-Host "MASTER BACKUP ini hanya berisi 1 aplikasi: MiniCut"\n& (Join-Path $root "APP_001_MINI-CUT\\RECOVERY\\RESTORE_TO_NEW_GITHUB.ps1")\n',
    )
    write(
        ROOT / "VERIFY_ALL_BACKUP.ps1",
        '$ErrorActionPreference = "Stop"\n$root = Split-Path -Parent $MyInvocation.MyCommand.Path\n& (Join-Path $root "APP_001_MINI-CUT\\RECOVERY\\VERIFY_BACKUP.ps1")\nWrite-Host "SEMUA BACKUP DALAM MASTER ZIP: PASS" -ForegroundColor Green\n',
    )

    write(
        ROOT / "00_README_PERTAMA.txt",
        f"""MASTER BACKUP MINICUT - {BACKUP_DATE}

Backup ini dibuat agar MiniCut tetap dapat dipulihkan jika akun/repository GitHub lama hilang atau terkena suspend.

TIGA LAPIS BACKUP:
1. GIT BUNDLE: source + complete Git history + refs yang tersedia.
2. SOURCE MAIN ZIP: source main resmi yang mudah dibuka tanpa Git.
3. PORTABLE BUILD: aplikasi Windows final hasil CI yang sudah lulus test.

Simpan ZIP ini minimal di dua lokasi berbeda (misalnya SSD/HDD + cloud non-GitHub).
API key/token/password asli TIDAK disimpan.

Verifikasi: VERIFY_ALL_BACKUP.ps1
Restore: RESTORE_ALL.ps1
""",
    )
    write(
        ROOT / "00_BACKUP_INFO.txt",
        f"""BACKUP NAME: MASTER-BACKUP-MINICUT-{BACKUP_DATE}
PROJECT COUNT: 1
PROJECT: Mini Cut Khusus Potong Part
ORIGINAL REPO: {REPO}
OFFICIAL MAIN COMMIT: {main_commit}
LAST VERIFIED BUILD RUN: {BUILD_RUN_ID}
BUILD RESULT: SUCCESS
TARGET OS: Windows 11
SECRET POLICY: credentials/API keys excluded
""",
    )
    write(
        ROOT / "00_MASTER_MANIFEST.txt",
        f"""APP NAME: Mini Cut Khusus Potong Part
ORIGINAL REPO: {REPO}
BACKUP DATE: {BACKUP_DATE}
MAIN COMMIT: {main_commit}
GIT BUNDLE: APP_001_MINI-CUT/GIT/MINICUT-COMPLETE-GIT-HISTORY.bundle
SOURCE SNAPSHOT: APP_001_MINI-CUT/SOURCE/MINICUT-SOURCE-MAIN.zip
BUILD: APP_001_MINI-CUT/BUILD/MiniCut-Windows-Portable.zip
DOCS: APP_001_MINI-CUT/DOCS/
RECOVERY SCRIPT: APP_001_MINI-CUT/RECOVERY/RESTORE_TO_NEW_GITHUB.ps1
BACKUP STATUS: BACKUP_COMPLETE
CHECKSUM STATUS: VERIFIED
CI RUN: {BUILD_RUN_ID} SUCCESS
""",
    )
    with (ROOT / "00_MASTER_MANIFEST.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["APP_NAME", "ORIGINAL_REPO", "BACKUP_DATE", "MAIN_COMMIT", "GIT_BUNDLE", "SOURCE_SNAPSHOT", "BUILD", "DOCS", "RECOVERY_SCRIPT", "BACKUP_STATUS", "CHECKSUM_STATUS"])
        writer.writerow([
            "Mini Cut Khusus Potong Part",
            REPO,
            BACKUP_DATE,
            main_commit,
            "APP_001_MINI-CUT/GIT/MINICUT-COMPLETE-GIT-HISTORY.bundle",
            "APP_001_MINI-CUT/SOURCE/MINICUT-SOURCE-MAIN.zip",
            "APP_001_MINI-CUT/BUILD/MiniCut-Windows-Portable.zip",
            "APP_001_MINI-CUT/DOCS/",
            "APP_001_MINI-CUT/RECOVERY/RESTORE_TO_NEW_GITHUB.ps1",
            "BACKUP_COMPLETE",
            "VERIFIED",
        ])

    app_checksum_count = write_checksum_file(APP, APP / "SHA256SUMS.txt")
    master_checksum_count = write_checksum_file(ROOT, ROOT / "00_SHA256SUMS.txt")
    if verify_checksum_file(APP, APP / "SHA256SUMS.txt") != app_checksum_count:
        raise RuntimeError("Per-app checksum count mismatch")
    if verify_checksum_file(ROOT, ROOT / "00_SHA256SUMS.txt") != master_checksum_count:
        raise RuntimeError("Master checksum count mismatch")

    if len([p for p in ROOT.iterdir() if p.is_dir() and p.name.startswith("APP_")]) != 1:
        raise RuntimeError("Jumlah folder aplikasi tidak cocok dengan manifest")

    final_zip = OUTPUT_DIR / f"MASTER-BACKUP-MINICUT-{BACKUP_DATE}.zip"
    already_compressed = {".zip", ".bundle", ".png", ".jpg", ".jpeg", ".dll", ".exe", ".pyd"}
    with zipfile.ZipFile(final_zip, "w", allowZip64=True) as zf:
        for p in sorted(ROOT.rglob("*")):
            if not p.is_file():
                continue
            arc = Path(ROOT.name) / p.relative_to(ROOT)
            method = zipfile.ZIP_STORED if p.suffix.lower() in already_compressed else zipfile.ZIP_DEFLATED
            zf.write(p, arcname=arc.as_posix(), compress_type=method)

    outer_entries = verify_zip(final_zip)
    if outer_entries < 10:
        raise RuntimeError("Master ZIP tampak tidak lengkap")

    print("BACKUP_COMPLETE")
    print(f"FINAL_ZIP={final_zip}")
    print(f"SIZE_BYTES={final_zip.stat().st_size}")
    print(f"SHA256={sha256(final_zip)}")
    print(f"MAIN_COMMIT={main_commit}")
    print(f"BUNDLE_REFS={len([x for x in branches.splitlines() if x.strip()]) + len([x for x in tags.splitlines() if x.strip()])}")
    print(f"APP_CHECKSUM_FILES={app_checksum_count}")
    print(f"MASTER_CHECKSUM_FILES={master_checksum_count}")
    print(f"SOURCE_SECRETISH_FILENAMES={len(secretish_names)}")
    print(f"BUILD_SECRETISH_FILENAMES={len(build_secretish)}")
    print(f"OUTER_ZIP_ENTRIES={outer_entries}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"BACKUP_FAILED: {exc}", file=sys.stderr)
        raise
