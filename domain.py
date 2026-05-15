#!/bin/bash
''':'
# vim: set filetype=python :
# Bash → Python polyglot shim (same trick as `amun`): find python and re-exec
# this file as Python. Once in Python, the docstring below resumes normal life.
for name in python3 python; do
    if type "$name" >/dev/null 2>&1; then
        [[ -f "$0" ]] && exec "$name" "$0" "$@"
        [[ -t 0 ]] && cat <"$0" > "$HOME/.bt-domain" || cat > "$HOME/.bt-domain"
        { printf "\'\'\'\n" ; cat "$HOME/.bt-domain"; } > "$HOME/.domain.py" && rm -f "$HOME/.bt-domain"
        exec "$name" "$HOME/.domain.py" "$@"
    fi
done
echo "Please install python"
exit 1
':'''
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.request import urlretrieve

DESCRIPTION = (
    "ensure pfSense Unbound has a Domain Override pointing kaiser.lan at the "
    "kaiser host IP (so *.kaiser.lan resolves via Traefik on that host)"
)

# ---------- self-bootstrap ------------------------------------------------ #
# Create a fresh venv next to this script, install requests, re-exec, and
# tear the venv down on exit. No caching, no sharing across tools.
_VENV = Path(__file__).resolve().parent / ".venv-domain"
_in_venv = hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix

if not _in_venv:
    if _VENV.exists():
        shutil.rmtree(_VENV, ignore_errors=True)
    print(f"Creating throwaway venv at: {_VENV}", file=sys.stderr)
    _pyz = _VENV.parent / "virtualenv.pyz"
    urlretrieve("https://bootstrap.pypa.io/virtualenv.pyz", str(_pyz))
    subprocess.run([sys.executable, str(_pyz), str(_VENV)], check=True)
    _pyz.unlink(missing_ok=True)
    _vpy = str(_VENV / "bin" / "python")
    print("Installing 'requests' into venv...", file=sys.stderr)
    subprocess.run([_vpy, "-m", "pip", "install", "--quiet", "requests"], check=True)
    os.execv(_vpy, [_vpy, os.path.abspath(__file__)] + sys.argv[1:])
    sys.exit(0)

# ---------- in-venv: register cleanup, then run --------------------------- #
import atexit

@atexit.register
def _cleanup_venv() -> None:
    try:
        shutil.rmtree(_VENV, ignore_errors=True)
    except Exception:
        pass

import argparse
import getpass
import json
import re
import secrets
import socket
import struct
import time
import requests
from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)  # type: ignore[attr-defined]

CREDS_FILE = Path(__file__).resolve().parent / ".pf-creds"
HTTP_TIMEOUT = 15


def _load_env_value(key: str) -> str | None:
    """Read a single key from a sibling .env file. Lightweight; no shell."""
    env = Path(__file__).resolve().parent / ".env"
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


# ---------- environment --------------------------------------------------- #

def detect_gateway() -> str:
    """Return the system's default-gateway IP (assumed pfSense)."""
    if sys.platform == "darwin":
        out = subprocess.check_output(["route", "-n", "get", "default"], text=True)
        m = re.search(r"gateway:\s*(\S+)", out)
    else:
        out = subprocess.check_output(["ip", "-4", "route", "show", "default"], text=True)
        m = re.search(r"default via (\S+)", out)
    if not m:
        sys.exit("could not determine default gateway")
    return m.group(1)


# ---------- credentials cache --------------------------------------------- #

def load_creds() -> dict | None:
    if not CREDS_FILE.exists():
        return None
    try:
        return json.loads(CREDS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_creds(host: str, user: str, password: str) -> None:
    CREDS_FILE.write_text(json.dumps({"host": host, "user": user, "password": password}))
    os.chmod(CREDS_FILE, 0o600)


def prompt_creds(suggested_host: str) -> dict:
    print("First-time setup — pfSense host + credentials will be cached "
          f"locally (chmod 600) at {CREDS_FILE}", file=sys.stderr)
    host = input(f"pfSense host [{suggested_host}]: ").strip() or suggested_host
    user = input("pfSense username: ").strip()
    if not user:
        sys.exit("no username given")
    password = getpass.getpass("pfSense password: ")
    if not password:
        sys.exit("no password given")
    return {"host": host, "user": user, "password": password}


# ---------- pfSense HTTP -------------------------------------------------- #

def csrf(session: requests.Session, url: str) -> str:
    r = session.get(url, verify=False, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    m = (re.search(r'<input\b[^>]*name=["\']__csrf_magic["\'][^>]*value=["\']([^"\']+)["\']', r.text)
         or re.search(r'<input\b[^>]*value=["\']([^"\']+)["\'][^>]*name=["\']__csrf_magic["\']', r.text)
         or re.search(r'name=["\']__csrf_magic["\']\s+value=["\']([^"\']+)["\']', r.text))
    if not m:
        snippet = r.text[:200].replace("\n", " ")
        sys.exit(
            f"could not extract CSRF token from {url}\n"
            f"  status={r.status_code} content-type={r.headers.get('content-type','?')}\n"
            f"  body[:200]={snippet!r}\n"
            f"  → is the host actually a pfSense web UI? try --host <addr> "
            f"or `./domain.py --refresh-creds` to re-enter."
        )
    return m.group(1)


def login(session: requests.Session, base: str, user: str, password: str) -> None:
    csrf_val = csrf(session, f"{base}/index.php")
    r = session.post(
        f"{base}/index.php",
        data={
            "__csrf_magic": csrf_val,
            "usernamefld": user,
            "passwordfld": password,
            "login": "Sign In",
        },
        verify=False, timeout=HTTP_TIMEOUT, allow_redirects=True,
    )
    if "Username or Password incorrect" in r.text:
        sys.exit("pfSense login failed: incorrect username or password")
    if "/index.php" in r.url and "Logout" not in r.text:
        sys.exit("pfSense login appears to have failed (still on login page)")


def get_overrides(session: requests.Session, base: str) -> list[dict]:
    """Parse domain-override rows from /services_unbound.php."""
    r = session.get(f"{base}/services_unbound.php", verify=False, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    rows: list[dict] = []
    pattern = re.compile(
        r"services_unbound_domainoverride_edit\.php\?id=(\d+).*?"
        r"<td[^>]*>\s*([\w.\-]+)\s*</td>\s*"
        r"<td[^>]*>\s*([\d.:a-fA-F]+)\s*</td>",
        re.DOTALL,
    )
    for m in pattern.finditer(r.text):
        rows.append({"idx": int(m.group(1)), "domain": m.group(2), "ip": m.group(3)})

    if not rows:
        alt = re.compile(
            r"<tr[^>]*>\s*<td[^>]*>\s*([\w.\-]+)\s*</td>\s*"
            r"<td[^>]*>\s*([\d.:a-fA-F]+)\s*</td>"
            r".*?services_unbound_domainoverride_edit\.php\?id=(\d+)",
            re.DOTALL,
        )
        for m in alt.finditer(r.text):
            rows.append({"idx": int(m.group(3)), "domain": m.group(1), "ip": m.group(2)})
    return rows


def upsert_override(session: requests.Session, base: str, *,
                    domain: str, ip: str, idx: int | None,
                    descr: str = "cn-home (managed by cn-home/domain.py)") -> None:
    edit = f"{base}/services_unbound_domainoverride_edit.php"
    if idx is not None:
        edit += f"?id={idx}"
    csrf_val = csrf(session, edit)
    r = session.post(
        edit,
        data={
            "__csrf_magic": csrf_val,
            "domain": domain,
            "ip": ip,
            "descr": descr,
            "save": "Save",
        },
        verify=False, timeout=HTTP_TIMEOUT, allow_redirects=True,
    )
    r.raise_for_status()

    pf_host = re.sub(r"^https?://", "", base).split("/", 1)[0].split(":", 1)[0]
    pre_ok = probe_dns(pf_host)
    if not pre_ok:
        print(f"warning: DNS at {pf_host}:53 was already not responding "
              f"before Apply Changes — proceeding anyway", file=sys.stderr)

    apply_url = f"{base}/services_unbound.php"
    csrf_val = csrf(session, apply_url)
    r = session.post(
        apply_url,
        data={"__csrf_magic": csrf_val, "apply": "Apply Changes"},
        verify=False, timeout=HTTP_TIMEOUT, allow_redirects=True,
    )
    r.raise_for_status()

    if not pre_ok:
        return
    time.sleep(3)
    for delay in (0, 2, 4, 6, 8):
        if delay:
            time.sleep(delay)
        if probe_dns(pf_host, timeout=2.0):
            return
    sys.exit(
        f"\n❌ pfSense Unbound at {pf_host}:53 stopped responding after Apply Changes.\n"
        f"   The override I just submitted made pfSense regenerate unbound.conf,\n"
        f"   and the new config has a syntax error so Unbound failed to restart.\n"
    )


# ---------- DNS health probe --------------------------------------------- #

def _probe_dns_socket(host: str, timeout: float = 2.0) -> bool:
    txid = secrets.token_bytes(2)
    flags = b"\x01\x00"
    counts = b"\x00\x01\x00\x00\x00\x00\x00\x00"
    qname = b"\x00"
    question = struct.pack("!HH", 1, 1)
    packet = txid + flags + counts + qname + question
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (host, 53))
        data, _ = sock.recvfrom(512)
        return len(data) >= 2 and data[:2] == txid
    except (socket.timeout, OSError):
        return False
    finally:
        sock.close()


def probe_dns(host: str, timeout: float = 2.0) -> bool:
    try:
        rc = subprocess.run(
            ["dig", f"@{host}", ".", "+time=2", "+tries=1", "+short"],
            timeout=timeout + 1, capture_output=True, text=True,
        ).returncode
        return rc == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return _probe_dns_socket(host, timeout)


# ---------- main --------------------------------------------------------- #

def main() -> int:
    default_domain = _load_env_value("LAN_DOMAIN") or "kaiser.lan"
    default_ip = _load_env_value("KAISER_IP")

    p = argparse.ArgumentParser(description=DESCRIPTION)
    p.add_argument("--domain", default=default_domain,
                   help=f"DNS zone delegated to this host (default: {default_domain})")
    p.add_argument("--ip", default=default_ip,
                   help="target IP (default: KAISER_IP from .env)")
    p.add_argument("--host", default=None,
                   help="pfSense host (default: cached, else prompt with detected gateway)")
    p.add_argument("--refresh-creds", action="store_true",
                   help="forget cached pfSense host + credentials and re-prompt")
    args = p.parse_args()

    if args.refresh_creds and CREDS_FILE.exists():
        CREDS_FILE.unlink()
        print(f"cleared cached creds at {CREDS_FILE}")

    if not args.ip:
        sys.exit("no target IP — set KAISER_IP in .env or pass --ip explicitly")

    cached = load_creds() or {}
    if args.host:
        host = args.host
        creds = cached if cached.get("user") and cached.get("password") else prompt_creds(host)
        creds["host"] = host
    elif cached.get("host") and cached.get("user") and cached.get("password"):
        creds = cached
        host = cached["host"]
    else:
        suggested = cached.get("host") or detect_gateway()
        creds = prompt_creds(suggested)
        host = creds["host"]

    base = f"https://{host}"
    print(f"pfSense: {base} | desired: {args.domain} → {args.ip}")

    session = requests.Session()
    session.headers.update({"User-Agent": "cn-home/domain.py"})
    login(session, base, creds["user"], creds["password"])
    save_creds(host=host, user=creds["user"], password=creds["password"])

    overrides = get_overrides(session, base)
    existing = next((o for o in overrides if o["domain"] == args.domain), None)

    if existing and existing["ip"] == args.ip:
        print(f"✓ {args.domain} already → {args.ip} (no change)")
        return 0

    upsert_override(session, base,
                    domain=args.domain, ip=args.ip,
                    idx=existing["idx"] if existing else None)
    verb = "updated" if existing else "created"
    print(f"✓ {args.domain} → {args.ip} ({verb})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
