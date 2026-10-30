# -*- coding: utf-8 -*-
"""The root certificates Windows trusts for HTTPS, as one PEM file for Node.js (npm).

WHY
A network that inspects HTTPS (common on managed PCs) re-signs every site with the
organisation's own root certificate. IT puts that root into Windows' certificate store, so
browsers, PowerShell and the codex CLI trust it — but Node.js checks against its own
built-in list, so npm fails with UNABLE_TO_GET_ISSUER_CERT_LOCALLY (a field report on
1.25.2: `npm install -g @openai/codex` on such a network). NODE_EXTRA_CA_CERTS pointed at
this file adds Windows' roots to Node's list on every Node.js since 7.3; a newer Node.js can
also read the store itself (NODE_USE_SYSTEM_CA), which the callers set as well.

WHAT GOES IN — only what Windows itself would accept for a server certificate:
- ssl.enum_certificates() reads every store location (the user's, the machine's, Group
  Policy, Enterprise) and reports each root's EFFECTIVE purposes, the restrictions IT or the
  root program set on the store entry included. A root limited to code signing, or switched
  off, stays out: writing its bare certificate would hand Node a trust Windows withholds.
- A root that is also in the Disallowed store stays out.
- Each one has to load in OpenSSL, as Node's own loader will: Node stops reading the file at
  the first certificate it can't parse, which would drop every root after it.
Nothing is switched off anywhere: npm still verifies every certificate, against what this PC
already trusts. Certificates are public, so the file holds nothing secret.

WHO
install-codex.cmd runs this file (`python windowsca.py <out.pem>`, exit 0 written / 1 not)
and cliupdate imports it, so one implementation writes the one file. Stdlib only."""

import os
import ssl
import sys
import tempfile
from pathlib import Path

_SERVER_AUTH = "1.3.6.1.5.5.7.3.1"


def default_path():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "claude-overlay" / "windows-ca.pem"


def _store(name):
    try:
        return ssl.enum_certificates(name)
    except Exception:
        return []


def roots():
    """DER bytes of each root Windows trusts for server authentication, deduplicated, in
    store order — minus the Disallowed ones and any OpenSSL won't load."""
    disallowed = {der for der, _enc, _trust in _store("Disallowed")}
    try:
        probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return []
    out, seen = [], set()
    for der, enc, trust in _store("ROOT"):
        if enc != "x509_asn" or der in seen or der in disallowed:
            continue
        if not (trust is True or _SERVER_AUTH in trust):
            continue
        seen.add(der)
        try:
            probe.load_verify_locations(cadata=der)
        except Exception:
            continue
        out.append(der)
    return out


def write(out=None):
    """Write the PEM file and return its path, or None when there was nothing to write or it
    couldn't be. Never raises. A temporary file of its own beside the target, then os.replace:
    a reader sees the old file or the new one, never a missing or half-written one, and two
    writers at once don't collide."""
    out = Path(out) if out else default_path()
    try:
        certs = roots()
        if not certs:
            return None
        out.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=out.name + ".", suffix=".partial", dir=out.parent)
        try:
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
                f.write("".join(ssl.DER_cert_to_PEM_cert(der) for der in certs))
            try:
                os.replace(tmp, out)
            except OSError:
                # Held open by an npm reading it this moment: that copy is as good.
                if not out.is_file():
                    return None
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return str(out)
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(0 if len(sys.argv) == 2 and write(sys.argv[1]) else 1)
