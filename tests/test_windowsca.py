"""windowsca -- the roots Windows trusts for HTTPS, as one PEM file for npm.

Field report behind it (1.25.2): `npm install -g @openai/codex` failed with
UNABLE_TO_GET_ISSUER_CERT_LOCALLY on a network that inspects HTTPS. That network's root is in
Windows' store, so browsers and the codex CLI trust it; Node.js checks its own list. Handed
to npm as NODE_EXTRA_CA_CERTS, this file fixes that without switching anything off -- and
must not hand Node a trust Windows withholds.

The store-reading tests run for real (they only read the certificate store and write into
tmp); the filtering tests feed it a store of their own.
"""
import os
import subprocess
import sys

import pytest

import windowsca

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows certificate store")


def openssl_loads(pem):
    """How many certificates OpenSSL takes from the file -- in a `python -S` child, because
    an installed truststore replaces ssl.SSLContext with one that can't count."""
    code = ("import ssl, sys; c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); "
            "c.load_verify_locations(cafile=sys.argv[1]); print(c.cert_store_stats()['x509'])")
    p = subprocess.run([sys.executable, "-S", "-c", code, str(pem)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return int(p.stdout)


@pytest.fixture(scope="module")
def real_roots():
    import ssl
    ders = [d for d, enc, trust in ssl.enum_certificates("ROOT")
            if enc == "x509_asn" and trust is True]
    if len(ders) < 4:
        pytest.skip("needs four all-purpose roots in this machine's store")
    return ders[:4]


@windows
def test_every_root_it_writes_loads(tmp_path):
    """Node.js reads the file with OpenSSL, and stops at the first block it can't parse."""
    out = tmp_path / "new folder" / "windows-ca.pem"
    assert windowsca.write(out) == str(out)
    text = out.read_text(encoding="ascii")
    blocks = text.count("-----BEGIN CERTIFICATE-----")
    assert blocks > 0 and openssl_loads(out) == blocks
    assert os.listdir(out.parent) == ["windows-ca.pem"]          # no temporary left behind


@windows
def test_only_what_windows_accepts_for_https_goes_in(monkeypatch, real_roots):
    all_purpose, server_auth, code_signing, switched_off = real_roots
    disallowed = real_roots[0] + b""                               # same bytes, own object
    not_a_cert = b"\x30\x03\x02\x01\x00"                           # ASN.1, but no certificate
    store = {
        "ROOT": [(not_a_cert, "x509_asn", True),                   # first: mustn't stop the rest
                 (all_purpose, "x509_asn", True),
                 (server_auth, "x509_asn", {windowsca._SERVER_AUTH, "1.3.6.1.5.5.7.3.2"}),
                 (code_signing, "x509_asn", {"1.3.6.1.5.5.7.3.3"}),
                 (switched_off, "x509_asn", set()),
                 (server_auth, "x509_asn", True),                  # listed twice
                 (b"crl", "pkcs_7_asn", True)],
        "Disallowed": [],
    }
    monkeypatch.setattr(windowsca, "_store", lambda name: store.get(name, []))
    assert windowsca.roots() == [all_purpose, server_auth]
    store["Disallowed"] = [(disallowed, "x509_asn", True)]
    assert windowsca.roots() == [server_auth]


def test_nothing_to_write_or_nowhere_to_write_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(windowsca, "roots", lambda: [])
    assert windowsca.write(tmp_path / "x.pem") is None and not (tmp_path / "x.pem").exists()
    monkeypatch.setattr(windowsca, "roots", lambda: [b"x"])
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")
    assert windowsca.write(blocker / "x.pem") is None


@windows
def test_a_file_held_open_keeps_the_copy_that_is_there(monkeypatch, tmp_path, real_roots):
    """os.replace fails while a reader holds the file; the copy it reads is as good."""
    out = tmp_path / "windows-ca.pem"
    out.write_text("earlier copy")
    monkeypatch.setattr(windowsca, "roots", lambda: real_roots[:1])

    def busy(src, dst):
        raise PermissionError("in use")
    monkeypatch.setattr(windowsca.os, "replace", busy)
    assert windowsca.write(out) == str(out)
    assert out.read_text() == "earlier copy"
    assert os.listdir(tmp_path) == ["windows-ca.pem"]
    out.unlink()
    assert windowsca.write(out) is None and os.listdir(tmp_path) == []


@windows
def test_run_as_a_script_it_says_so_with_its_exit_code(tmp_path):
    script = os.path.join(ROOT, "windowsca.py")
    ok = subprocess.run([sys.executable, script, str(tmp_path / "ca.pem")], timeout=60)
    assert ok.returncode == 0 and (tmp_path / "ca.pem").is_file()
    assert subprocess.run([sys.executable, script], timeout=60).returncode == 1


def test_nothing_turns_certificate_checking_off():
    for name in ("windowsca.py", "install-codex.cmd", "cliupdate.py"):
        text = open(os.path.join(ROOT, name), encoding="utf-8").read().lower()
        for off in ("strict-ssl", "strict_ssl", "node_tls_reject_unauthorized"):
            assert off not in text, (name, off)
