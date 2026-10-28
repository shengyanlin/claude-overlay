"""install-node.ps1 -- the no-admin Node.js install install-codex.cmd tries before winget.

Field report behind it: a 1.25.0 -> 1.25.1 update on a managed PC with no Node.js. The only
Node.js route was winget's machine-wide MSI, which needs an admin to approve it; the setup
"seemed to install" and the window closed. The official .zip needs no installer and no admin.

Read from the text rather than executed, like test_install_python_ps1.py and for the same
reason: running it installs Node.js. (It was run for real into a throwaway LOCALAPPDATA with
-NoUserPath while being written: the LTS lookup, checksum, unpack and npm prefix all held.)
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "install-node.ps1")


@pytest.fixture(scope="module")
def text():
    return open(SCRIPT, encoding="ascii").read()


@pytest.fixture(scope="module")
def code(text):
    """Executable lines only: the <# #> header and # comments describe, they don't do."""
    out, in_block = [], False
    for line in text.splitlines():
        s = line.strip()
        if in_block:
            in_block = "#>" not in s
            continue
        if s.startswith("<#"):
            in_block = "#>" not in s[2:]
            continue
        if s.startswith("#") or not s:
            continue
        out.append(line)
    return "\n".join(out)


def test_the_script_is_ascii_and_bom_free():
    raw = open(SCRIPT, "rb").read()
    assert not raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM"
    bad = [i for i, b in enumerate(raw) if b > 127]
    assert not bad, f"non-ASCII byte at offset {bad[0]}"


def test_it_installs_where_install_codex_and_the_overlay_look(code):
    assert "Join-Path $env:LOCALAPPDATA 'Programs\\nodejs'" in code
    cmd = open(os.path.join(ROOT, "install-codex.cmd"), encoding="ascii").read()
    assert "%LOCALAPPDATA%\\Programs\\nodejs" in cmd
    worker = open(os.path.join(ROOT, "codexworker.py"), encoding="utf-8").read()
    assert '_env_dir("LOCALAPPDATA", "Programs", "nodejs")' in worker


def test_the_zip_is_refused_unless_its_checksum_matches(code):
    """The checksum is checked before anything is unpacked; a mismatch is never unpacked."""
    assert "SHASUMS256.txt" in code
    i_hash = code.index("Get-FileHash -Algorithm SHA256")
    assert i_hash < code.index("tar.exe -xf")
    mismatch = code[code.index("if ($got -ne $want) {"):]
    assert mismatch.index("continue") < mismatch.index("$ver, $name, $zip = $v, $n, $z")
    assert re.search(r"if \(-not \$ver\) \{.*?Fail ", code, re.S)


def test_a_release_still_being_published_is_skipped_for_the_one_before(code):
    """Measured: the day after 26.x became LTS, index.json listed it and its .zip answered
    200 while its SHASUMS256.txt was a 404. The newest few LTS releases are candidates."""
    assert re.search(r"\$_\.lts -and \(\$_\.files -contains \"win-\$arch-zip\"\)", code)
    assert "Select-Object -First 3" in code
    loop = code[code.index("foreach ($rel in $cands) {\n    $v"):]
    assert loop.index('"$Dist/$v/SHASUMS256.txt"') < loop.index('"$Dist/$v/$n.zip"')
    assert loop.count("continue") >= 4


def test_it_unpacks_beside_the_target_and_moves_in_whole(code):
    """A half-unpacked tree at the target would be found by the next run and trusted."""
    assert re.search(r'tar\.exe -xf "\$zip" -C "\$stage"', code)
    assert "ExtractToDirectory($zip, $stage)" in code
    assert "$tree = Join-Path $stage $name" in code
    assert "Move-Item -LiteralPath $tree -Destination $NodeRoot" in code
    assert "-C \"$NodeRoot\"" not in code


def test_an_existing_folder_is_moved_aside_not_deleted(code):
    assert "Rename-Item -LiteralPath $NodeRoot" in code
    assert not re.search(r"Remove-Item\s+(-LiteralPath\s+)?\$NodeRoot\b", code)


def test_npm_gets_the_msi_global_prefix(code):
    """Globals in %APPDATA%\\npm, where the overlay's codex lookup and update.cmd look."""
    assert "'prefix=${APPDATA}\\npm'" in code


def test_the_user_path_is_edited_without_flattening_it(code):
    """[Environment]::SetEnvironmentVariable('Path', .., 'User') reads the Path expanded and
    writes it back as REG_SZ, freezing every %VAR% entry the user had."""
    assert "DoNotExpandEnvironmentNames" in code
    assert "RegistryValueKind]::ExpandString" in code
    assert not re.search(r"SetEnvironmentVariable\('Path'", code)
    assert "if (-not $NoUserPath) { Add-UserPath" in code


def test_curl_is_preferred_but_not_required(code):
    assert "Get-Command curl.exe" in code
    assert "Invoke-WebRequest -Uri $url -OutFile $dst" in code


def test_success_means_node_and_npm_both_run(code):
    assert "& $node --version" in code and "& $npm --version" in code


def test_nothing_that_does_not_run_is_left_where_npm_is_looked_for(code):
    """A node.exe blocked by device policy must not leave an npm.cmd at the target (where
    install-codex.cmd's `where npm` would trust it over winget) or on the user Path."""
    i_test = code.index("if (-not (Test-Node $tree))")
    assert i_test < code.index("Move-Item -LiteralPath $tree -Destination $NodeRoot")
    assert i_test < code.index("Add-UserPath @($NodeRoot, $NpmBin) }\n  Write-Host")
    assert code.index("Join-Path $tree 'node_modules\\npm\\npmrc'") < i_test
    after_move = code[code.index("if (-not (Test-Node $NodeRoot)) {"):]
    assert after_move.index("Rename-Item -LiteralPath $NodeRoot") < after_move.index("Fail ")
