"""cliupdate's certificate file for npm on a Mac (NODE_EXTRA_CA_CERTS) — windowsca's
counterpart, and what test_windowsca.py is to that.

A keychain stores certificates; it doesn't say which are trusted. The file is handed to
Node.js as roots to trust, so a certificate goes in only if it is a root CA AND macOS itself
accepts it for an HTTPS server (`security verify-cert -p ssl`: the admin's and the user's
trust settings). Anything else — not trusted, not a root, `security` failing or slow — stays
out, and a failure writes no file at all rather than part of one.

The logic runs everywhere against a stand-in for `security`; what calls the real one is
macOS-only. Nothing touches a keychain, trust settings, the network or the real
~/Library/Application Support: the data folder is a temp one throughout."""
import os
import ssl
import subprocess
import sys

import pytest

import cliupdate as cu
import config

mac = pytest.mark.skipif(sys.platform != "darwin", reason="macOS `security`")

# Throwaway P-256 certificates made with /usr/bin/openssl for these tests; their keys were
# deleted and nothing trusts them. Two root CAs, and three that are NOT roots: a leaf and an
# intermediate CA issued by root A, and a self-signed certificate that isn't a CA.
ROOT_A = """-----BEGIN CERTIFICATE-----
MIIBZjCCAQygAwIBAgIJAIpBJHL7sZ+VMAoGCCqGSM49BAMCMCUxIzAhBgNVBAMM
GmNsYXVkZS1vdmVybGF5IHRlc3Qgcm9vdCBBMCAXDTI2MTExODE2NTUwMFoYDzIx
MjYxMDI1MTY1NTAwWjAlMSMwIQYDVQQDDBpjbGF1ZGUtb3ZlcmxheSB0ZXN0IHJv
b3QgQTBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABIQBVjl14/Cq2zFKQP8dRuE0
E1KqqdR0S24iprk4ujFZCxGO7xcIL7xLeo4BfTf5cCBEcnH/0IA7pgjmipqNlcaj
IzAhMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgEGMAoGCCqGSM49BAMC
A0gAMEUCIGygQynExaIZhWfKVGO1kjgJOo1j6Z0lJvTQ+pWodwtiAiEAm23pOaX3
wBXPuhuWjPdqLygRNRRrDcnkIf9LpwljGXQ=
-----END CERTIFICATE-----"""
ROOT_B = """-----BEGIN CERTIFICATE-----
MIIBZTCCAQygAwIBAgIJAP2sZTzXM3hxMAoGCCqGSM49BAMCMCUxIzAhBgNVBAMM
GmNsYXVkZS1vdmVybGF5IHRlc3Qgcm9vdCBCMCAXDTI2MTExODE2NTUwMFoYDzIx
MjYxMDI1MTY1NTAwWjAlMSMwIQYDVQQDDBpjbGF1ZGUtb3ZlcmxheSB0ZXN0IHJv
b3QgQjBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABBqjUPiTHF5xsao/gf6X981E
G5ouhASLkRRRdXncCQNvqezJ618Zdxc//PgflMFkKgpup3jXJSDgwruLdBwvKU2j
IzAhMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgEGMAoGCCqGSM49BAMC
A0cAMEQCIGQCMEfJw7Bobb26F0XyDEp/JRf8Yw+ZbyJZOQ/mNUpHAiBnFnVBINOF
CSit+BMpeTzqxhyQg/p8mhxH0MnRDLlpcw==
-----END CERTIFICATE-----"""
LEAF = """-----BEGIN CERTIFICATE-----
MIIBYTCCAQegAwIBAgIJALS4EfkqVKjhMAoGCCqGSM49BAMCMCUxIzAhBgNVBAMM
GmNsYXVkZS1vdmVybGF5IHRlc3Qgcm9vdCBBMCAXDTI2MTExODE2NTUwMFoYDzIx
MjYxMDI1MTY1NTAwWjAjMSEwHwYDVQQDDBhjbGF1ZGUtb3ZlcmxheSB0ZXN0IGxl
YWYwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAASJIk18h0wPkS7R+OyrFZmCUlSV
V6QJ2DJYJDg7QF8TYTw0wAOF6WMkH52rMTekqcYodJJuvRl23zWwY873ic2uoyAw
HjAMBgNVHRMBAf8EAjAAMA4GA1UdDwEB/wQEAwIHgDAKBggqhkjOPQQDAgNIADBF
AiEAx3AYQVduNUqJy04s125vWrdFdqrdYTxnSD1Lcgaztj8CIA/sfTGYE5wSKYGi
+/8ZIWvB9wWTen5b9k0qR+ZuFhyt
-----END CERTIFICATE-----"""
INTERMEDIATE = """-----BEGIN CERTIFICATE-----
MIIBbTCCARKgAwIBAgIJALS4EfkqVKjiMAoGCCqGSM49BAMCMCUxIzAhBgNVBAMM
GmNsYXVkZS1vdmVybGF5IHRlc3Qgcm9vdCBBMCAXDTI2MTExODE2NTUwMFoYDzIx
MjYxMDI1MTY1NTAwWjArMSkwJwYDVQQDDCBjbGF1ZGUtb3ZlcmxheSB0ZXN0IGlu
dGVybWVkaWF0ZTBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABFvnsd21Ltilr0kQ
BX7rgkZSieMF0zSreaiz5u91ZCtalaajsuRGwZOXHoVgmtOouNup6IcfB6URwBtU
p/Zmz72jIzAhMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgEGMAoGCCqG
SM49BAMCA0kAMEYCIQDXmq4YOqiX5BvOy23KADikZdqMCdA4waxDw8AA7O48NAIh
AJYPWCp3KI+5/yUyl+dEu2XgEMU9JnImc7n4yhXyPtwS
-----END CERTIFICATE-----"""
SELF_SIGNED_LEAF = """-----BEGIN CERTIFICATE-----
MIIBdzCCAR2gAwIBAgIJAMhBr/LQ/yvpMAoGCCqGSM49BAMCMC8xLTArBgNVBAMM
JGNsYXVkZS1vdmVybGF5IHRlc3Qgc2VsZi1zaWduZWQgbGVhZjAgFw0yNjExMTgx
NjU1MDBaGA8yMTI2MTAyNTE2NTUwMFowLzEtMCsGA1UEAwwkY2xhdWRlLW92ZXJs
YXkgdGVzdCBzZWxmLXNpZ25lZCBsZWFmMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcD
QgAEeqJ5QY9YeCFODhBgVoI+PaNsxQwAMhNpP4ltbb9STH/o8UdhDp5nKS1GggRI
w/rIHygBjQjGgw0Ton43N4Ev66MgMB4wDAYDVR0TAQH/BAIwADAOBgNVHQ8BAf8E
BAMCB4AwCgYIKoZIzj0EAwIDSAAwRQIgTHG43V9O6QM/jBi7pp6nZefp0inCXkG/
spP8/+VKjAoCIQDVs5vS7i1YnzYtgSQYWxhkV94/klMUJUAer3EcOmXgvw==
-----END CERTIFICATE-----"""


def der(pem):
    return ssl.PEM_cert_to_DER_cert(pem.strip())


def certs_in(path):
    """DER of each certificate in a PEM file, in order."""
    with open(path, encoding="ascii") as f:
        return [der(block) for block in cu._PEM_CERT.findall(f.read())]


@pytest.fixture(autouse=True)
def data(monkeypatch, tmp_path):
    """A temp data folder (never the real Application Support) and a fresh once-a-run cache."""
    home = tmp_path / "data"
    monkeypatch.setattr(config, "_data_home", lambda: home)
    monkeypatch.setattr(cu, "_ca_pem", None)
    return home / "claude-overlay"


class FakeSecurity:
    """subprocess.run, as `security` would answer it: `find-certificate` prints the
    keychain's certificates — every one of them, trusted or not, which is the point —
    and `verify-cert` exits 0 only for the ones in `trusted`."""

    def __init__(self, keychain, trusted=(), find_rc=0, on_verify=None):
        self.keychain, self.trusted = list(keychain), {der(p) for p in trusted}
        self.find_rc, self.on_verify = find_rc, on_verify
        self.found, self.verified = 0, []

    def __call__(self, args, **kw):
        assert args[0] == cu._SECURITY
        assert 0 < kw["timeout"] <= cu._MAC_CA_BUDGET        # nothing waits without limit
        done = lambda rc, out=b"": subprocess.CompletedProcess(args, rc, out, b"")
        if args[1] == "find-certificate":
            self.found += 1
            return done(self.find_rc, ("\n".join(self.keychain) + "\n").encode("ascii"))
        assert args[1] == "verify-cert"
        asked = certs_in(args[args.index("-c") + 1])
        assert len(asked) == 1                               # judged one at a time
        self.verified.append((asked[0], list(args)))
        if self.on_verify:
            self.on_verify(len(self.verified))
        return done(0 if asked[0] in self.trusted else 1)


def fake(monkeypatch, *a, **kw):
    sec = FakeSecurity(*a, **kw)
    monkeypatch.setattr(cu.subprocess, "run", sec)
    return sec


EVERYTHING = [ROOT_A, LEAF, ROOT_B, INTERMEDIATE, SELF_SIGNED_LEAF]


# ── what goes in: roots macOS trusts for HTTPS, and nothing else ────────────────────────

def test_only_the_roots_macos_trusts_are_written(monkeypatch, data):
    """The finding: root B sits in the keychain exactly as root A does, but macOS doesn't
    trust it — it must not become a root Node.js trusts."""
    fake(monkeypatch, EVERYTHING, trusted=[ROOT_A])
    pem = cu._mac_ca_pem()
    assert pem == str(data / "mac-ca.pem")
    assert certs_in(pem) == [der(ROOT_A)]
    assert os.listdir(data) == ["mac-ca.pem"]                # no .partial left beside it


def test_macos_is_asked_about_ssl_trust_without_the_network(monkeypatch):
    sec = fake(monkeypatch, [ROOT_A], trusted=[ROOT_A])
    assert cu._mac_ca_pem()
    (_cert, args), = sec.verified
    assert args[1] == "verify-cert" and args[args.index("-p") + 1] == "ssl"
    assert "-L" in args                                      # local only: nothing fetched


def test_a_certificate_macos_verifies_but_that_is_no_root_stays_out(monkeypatch):
    """macOS verifies a leaf or an intermediate issued under a trusted root too (seen on a
    managed Mac: its own identity certificates). They are not roots to trust anything by —
    so they aren't even put to `security`."""
    sec = fake(monkeypatch, EVERYTHING, trusted=EVERYTHING)
    assert certs_in(cu._mac_ca_pem()) == [der(ROOT_A), der(ROOT_B)]      # keychain order
    assert [cert for cert, _args in sec.verified] == [der(ROOT_A), der(ROOT_B)]


def test_a_root_in_two_keychains_is_written_once(monkeypatch):
    sec = fake(monkeypatch, [ROOT_B, ROOT_A, ROOT_B], trusted=[ROOT_A, ROOT_B])
    assert certs_in(cu._mac_ca_pem()) == [der(ROOT_B), der(ROOT_A)]
    assert len(sec.verified) == 2


def test_is_root_ca():
    assert cu._is_root_ca(der(ROOT_A)) and cu._is_root_ca(der(ROOT_B))
    assert not cu._is_root_ca(der(LEAF))
    assert not cu._is_root_ca(der(INTERMEDIATE))             # a CA, but issued by another
    assert not cu._is_root_ca(der(SELF_SIGNED_LEAF))         # self-signed, but not a CA
    assert not cu._is_root_ca(b"not a certificate")
    assert not cu._is_root_ca(der(ROOT_A) + der(ROOT_B))     # one certificate, not a bundle


def test_is_root_ca_under_truststore(monkeypatch):
    """On a Mac config.py lets truststore replace ssl.SSLContext with a subclass that
    refuses to list its certificates — which would have made every certificate "not a root"
    and the file silently never written."""
    class Replaced(ssl.SSLContext):
        def get_ca_certs(self, binary_form=False):
            raise NotImplementedError()
    monkeypatch.setattr(ssl, "SSLContext", Replaced)
    assert cu._is_root_ca(der(ROOT_A)) and not cu._is_root_ca(der(LEAF))


# ── nothing trusted, or anything going wrong: no file ───────────────────────────────────

def test_nothing_trusted_is_no_file(monkeypatch, data):
    fake(monkeypatch, EVERYTHING, trusted=[])
    assert cu._mac_ca_pem() is None
    assert not data.exists()


def test_an_empty_keychain_is_no_file(monkeypatch, data):
    sec = fake(monkeypatch, [], trusted=[])
    assert cu._mac_ca_pem() is None and sec.found == 1
    assert not data.exists()


def test_a_file_from_an_earlier_run_does_not_outlive_its_trust(monkeypatch, data):
    """Trusted last time, "Never Trust" now (or written by a build that didn't check)."""
    fake(monkeypatch, [ROOT_A], trusted=[ROOT_A])
    pem = cu._mac_ca_pem()
    assert os.path.isfile(pem)
    fake(monkeypatch, [ROOT_A], trusted=[])
    assert cu._mac_ca_pem() is None
    assert not os.path.exists(pem)


def test_find_certificate_failing_is_no_file(monkeypatch, data):
    sec = fake(monkeypatch, [ROOT_A], trusted=[ROOT_A], find_rc=1)
    assert cu._mac_ca_pem() is None
    assert not sec.verified and not data.exists()


def test_security_that_cannot_run_is_no_file(monkeypatch, data):
    def missing(args, **kw):
        raise FileNotFoundError(args[0])
    monkeypatch.setattr(cu.subprocess, "run", missing)
    assert cu._mac_ca_pem() is None
    assert not data.exists()


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("security", 1), OSError("spawn")])
def test_a_trust_check_that_fails_midway_writes_nothing_not_half(monkeypatch, data, error):
    """Root A was already verified when root B's check died: the file is every trusted root
    or it isn't written — and an earlier run's goes too."""
    data.mkdir(parents=True)
    (data / "mac-ca.pem").write_text(ROOT_B + "\n")

    def dies_on_the_second(n):
        if n == 2:
            raise error
    sec = fake(monkeypatch, [ROOT_A, ROOT_B], trusted=[ROOT_A, ROOT_B],
               on_verify=dies_on_the_second)
    assert cu._mac_ca_pem() is None
    assert len(sec.verified) == 2
    assert os.listdir(data) == []


def test_out_of_time_is_no_file(monkeypatch, data):
    """The calls share one budget; with none left `security` isn't even started."""
    sec = fake(monkeypatch, [ROOT_A], trusted=[ROOT_A])
    monkeypatch.setattr(cu, "_MAC_CA_BUDGET", 0)
    assert cu._mac_ca_pem() is None
    assert sec.found == 0 and not sec.verified and not data.exists()


def test_no_data_folder_is_none(monkeypatch):
    def broken():
        raise RuntimeError("no home")
    monkeypatch.setattr(config, "_data_home", broken)
    sec = fake(monkeypatch, [ROOT_A], trusted=[ROOT_A])
    assert cu._mac_ca_pem() is None and sec.found == 0


# ── npm's environment ───────────────────────────────────────────────────────────────────

@mac
def test_npm_gets_the_file_and_it_is_made_once_a_run(monkeypatch, data):
    sec = fake(monkeypatch, EVERYTHING, trusted=[ROOT_A])
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS", raising=False)
    monkeypatch.delenv("NODE_USE_SYSTEM_CA", raising=False)
    env = cu._npm_env()
    assert env["NODE_USE_SYSTEM_CA"] == "1"                  # a current Node.js: its own reading
    assert env["NODE_EXTRA_CA_CERTS"] == str(data / "mac-ca.pem")        # an older one: the file
    assert cu._npm_env()["NODE_EXTRA_CA_CERTS"] == env["NODE_EXTRA_CA_CERTS"]
    assert sec.found == 1


@mac
def test_npm_runs_without_a_file_when_nothing_is_trusted(monkeypatch, data):
    sec = fake(monkeypatch, EVERYTHING, trusted=[])
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS", raising=False)
    monkeypatch.delenv("NODE_USE_SYSTEM_CA", raising=False)
    for _ in range(2):
        env = cu._npm_env()
        assert "NODE_EXTRA_CA_CERTS" not in env and env["NODE_USE_SYSTEM_CA"] == "1"
    assert sec.found == 1                                    # not tried again this run


# ── the real `security` ─────────────────────────────────────────────────────────────────

@mac
def test_macos_does_not_trust_a_root_it_was_never_told_to(tmp_path):
    """The per-certificate check, for real: a brand-new self-signed CA that is in no keychain
    and has no trust settings is a root CA, and is not trusted."""
    if not os.access("/usr/bin/openssl", os.X_OK):
        pytest.skip("no /usr/bin/openssl")
    cnf, key, pem = tmp_path / "ca.cnf", tmp_path / "ca.key", tmp_path / "ca.pem"
    cnf.write_text("[req]\ndistinguished_name=dn\n[dn]\n[v3_ca]\n"
                   "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n")
    made = subprocess.run(
        ["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-subj", "/CN=claude-overlay test untrusted root", "-config", str(cnf),
         "-extensions", "v3_ca", "-keyout", str(key), "-out", str(pem)],
        capture_output=True, text=True, timeout=60)
    if made.returncode != 0 or not pem.is_file():
        pytest.skip("openssl couldn't make a certificate: " + made.stderr[-200:])
    key.unlink()
    (cert,) = certs_in(pem)
    assert cu._is_root_ca(cert)
    assert cu._mac_trusts_for_ssl(pem) is False


@mac
def test_macos_does_not_trust_these_tests_roots(tmp_path):
    for i, root in enumerate((ROOT_A, ROOT_B)):
        pem = tmp_path / f"root{i}.pem"
        pem.write_text(root + "\n")
        assert cu._mac_trusts_for_ssl(pem) is False


@mac
def test_what_this_mac_gets_is_all_trusted_roots(data):
    """End to end on whatever this Mac has: no file (nothing of the user's or the
    organisation's is trusted — a plain Mac), or a file in which every certificate is a root
    CA that macOS, asked again one by one, trusts for HTTPS."""
    pem = cu._mac_ca_pem()
    if pem is None:
        assert not (data / "mac-ca.pem").exists()
        return
    assert pem == str(data / "mac-ca.pem")
    certs = certs_in(pem)
    assert certs and len(set(certs)) == len(certs)
    one = data / "one.pem"
    for cert in certs:
        assert cu._is_root_ca(cert)
        one.write_text(ssl.DER_cert_to_PEM_cert(cert))
        assert cu._mac_trusts_for_ssl(one)


# ── install-codex.sh: the same file, from the same function ─────────────────────────────

@pytest.fixture
def codex_setup(tmp_path):
    """install-codex.sh in a temp app folder with a temp HOME (test_mac_scripts' harness),
    and an npm that records the certificate settings it was run with. → (run, npm_envs)"""
    from test_mac_scripts import Env, _app_folder
    env, folder, log = Env(tmp_path), _app_folder(tmp_path / "app"), tmp_path / "npm-env.txt"
    env.cli("npm", f'echo "SYSCA=$NODE_USE_SYSTEM_CA EXTRA=$NODE_EXTRA_CA_CERTS" >> "{log}"\n'
                   'case "$1" in prefix) echo "/nowhere" ;; esac\nexit 1\n')

    def run(**extra):
        r = env.run(folder / "install-codex.sh", "--from-setup", **extra)
        assert r.returncode == 3 and log.is_file(), r.stdout + r.stderr   # npm ran (and "failed")
        return env, folder

    return run, lambda: log.read_text().splitlines()


@mac
def test_install_codex_hands_npm_the_file_python_wrote(tmp_path, codex_setup):
    from test_mac_scripts import _exe
    run, npm_envs = codex_setup
    pem = tmp_path / "mac-ca.pem"
    pem.write_text(ROOT_A + "\n")
    python = _exe(tmp_path / "python", f'echo "$@" >> "{tmp_path}/py-calls.txt"\necho "{pem}"\n')
    run(OV_PYTHON=str(python))
    assert npm_envs()[0] == f"SYSCA=1 EXTRA={pem}"
    # One implementation: the script asks cliupdate, it has no certificate logic of its own.
    assert "cliupdate._mac_ca_pem()" in (tmp_path / "py-calls.txt").read_text()


@mac
@pytest.mark.parametrize("python_body", ["exit 1\n", "echo /no/such/file.pem\n", None])
def test_install_codex_still_installs_when_python_has_no_file(tmp_path, codex_setup,
                                                              python_body):
    """Nothing trusted (prints nothing), a file that isn't there, or no Python at all."""
    from test_mac_scripts import _exe
    run, npm_envs = codex_setup
    python = tmp_path / "python"
    if python_body:
        _exe(python, python_body)
    run(OV_PYTHON=str(python))
    assert npm_envs()[0] == "SYSCA=1 EXTRA="


@mac
def test_install_codex_keeps_certificate_settings_already_made(tmp_path, codex_setup):
    from test_mac_scripts import _exe
    run, npm_envs = codex_setup
    python = _exe(tmp_path / "python", f'touch "{tmp_path}/python-ran"\n')
    run(OV_PYTHON=str(python), NODE_EXTRA_CA_CERTS="/it/proxy-root.pem", NODE_USE_SYSTEM_CA="0")
    assert npm_envs()[0] == "SYSCA=0 EXTRA=/it/proxy-root.pem"
    assert not (tmp_path / "python-ran").exists()


@mac
def test_install_codex_with_the_real_python_and_cliupdate(tmp_path, codex_setup):
    """The script's own one-liner against the real cliupdate and the real `security`, HOME
    being a temp folder: npm gets no file, or one under that HOME holding only roots macOS
    trusts."""
    import shutil
    from test_mac_scripts import REPO
    run, npm_envs = codex_setup
    shutil.copy2(REPO / "cliupdate.py", tmp_path / "app" / "cliupdate.py")
    env, _folder = run(OV_PYTHON=sys.executable)
    sysca, extra = npm_envs()[0].split(" ", 1)
    assert sysca == "SYSCA=1"
    pem = extra[len("EXTRA="):]
    if not pem:
        assert not (env.state / "mac-ca.pem").exists()
        return
    assert os.path.samefile(pem, env.state / "mac-ca.pem")
    one = tmp_path / "one.pem"
    for cert in certs_in(pem):
        assert cu._is_root_ca(cert)
        one.write_text(ssl.DER_cert_to_PEM_cert(cert))
        assert cu._mac_trusts_for_ssl(one)
