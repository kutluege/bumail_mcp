#!/usr/bin/env python3
"""Manage a local GreenMail test server (IMAPS + SMTPS with a throwaway TLS cert).

Stdlib only.  Usage (from the repo root):

    uv run python scripts/greenmail.py certs            # keystore + .greenmail/ca.pem
    uv run python scripts/greenmail.py start            # jar mode unless docker is on PATH
    uv run python scripts/greenmail.py start --mode docker
    uv run python scripts/greenmail.py status
    uv run python scripts/greenmail.py capabilities     # print IMAP CAPABILITY over IMAPS
    uv run python scripts/greenmail.py stop

Parallel workers: give each its own instance (own ports, pid file, log; shared keystore/CA).
Either pass ``--instance NAME`` or export POSTA_GREENMAIL_INSTANCE=NAME (the tests read it too):

    export POSTA_GREENMAIL_INSTANCE=worker1
    uv run python scripts/greenmail.py start            # == start --instance worker1
    uv run pytest tests/integration -q -m greenmail
    uv run python scripts/greenmail.py stop

Everything generated lives in ``.greenmail/`` (gitignored): the downloaded jar, the PKCS12
keystore, the exported CA/cert PEM, the pid file and the log.  Nothing secret is real: the
keystore password and the user passwords below are obvious fakes for a throwaway test server.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GM_DIR = ROOT / ".greenmail"

# --- pinned versions --------------------------------------------------------------------
GREENMAIL_VERSION = "2.1.14"
JAR_NAME = f"greenmail-standalone-{GREENMAIL_VERSION}.jar"
JAR_URL = (
    "https://repo1.maven.org/maven2/com/icegreen/greenmail-standalone/"
    f"{GREENMAIL_VERSION}/{JAR_NAME}"
)
JAR_SHA256 = "0381392f3a44e4d8ae78051778440f58820d01acaf5757c3f43649deda8c1d23"
DOCKER_IMAGE = f"greenmail/standalone:{GREENMAIL_VERSION}"

# --- server layout ----------------------------------------------------------------------
HOST = "127.0.0.1"
IMAPS_PORT = 3993  # default instance
SMTPS_PORT = 3465
KEYSTORE = GM_DIR / "keystore.p12"
CA_PEM = GM_DIR / "ca.pem"
INSTANCE_ENV = "POSTA_GREENMAIL_INSTANCE"
# Named instances: slot = crc32(name) % INSTANCE_SLOTS; ports are INSTANCE_BASE + 3*slot + {0,1,2}
# = IMAPS, SMTPS, GreenMail REST API.  That is 13000..18999, clear of the default 3993/3465/8080.
INSTANCE_BASE = 13000
INSTANCE_SLOTS = 2000


@dataclass(frozen=True)
class Instance:
    name: str  # "" == default instance
    imaps_port: int
    smtps_port: int
    api_port: int | None  # None: GreenMail's own default (8080) for the default instance
    pid_file: Path
    log_file: Path
    docker_name: str


def get_instance(name: str | None = None) -> Instance:
    """Resolve an instance by name (None -> $POSTA_GREENMAIL_INSTANCE, empty -> default)."""
    if name is None:
        name = os.environ.get(INSTANCE_ENV, "")
    if not name:
        return Instance(
            "", IMAPS_PORT, SMTPS_PORT, None, GM_DIR / "greenmail.pid", GM_DIR / "greenmail.log",
            "posta-greenmail",
        )  # fmt: skip
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        sys.exit(f"error: bad instance name {name!r} (use letters, digits, _ or -)")
    base = INSTANCE_BASE + 3 * (zlib.crc32(name.encode()) % INSTANCE_SLOTS)
    return Instance(
        name, base, base + 1, base + 2, GM_DIR / f"greenmail-{name}.pid",
        GM_DIR / f"greenmail-{name}.log", f"posta-greenmail-{name}",
    )  # fmt: skip


@contextlib.contextmanager
def _lock(name: str, timeout: float = 180):
    """Cross-process lock via O_EXCL lock file (stale after `timeout` seconds)."""
    GM_DIR.mkdir(exist_ok=True)
    path = GM_DIR / f"{name}.lock"
    deadline = time.time() + timeout
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > timeout:
                    path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.time() > deadline:
                sys.exit(f"error: timed out waiting for lock {path}")
            time.sleep(0.2)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


# Obviously fake; protects nothing.  Keystore password == key password (PKCS12 requires it).
KEYSTORE_PASSWORD = "posta-test-keystore"

# login (== full e-mail address) -> fake password.  Fake domains use the reserved ``.test`` TLD.
USERS: dict[str, str] = {
    "ogrenci@std.bogazici.test": "ogrenci-fake-pass",  # a student
    "hoca@bogazici.test": "hoca-fake-pass",  # a faculty member
    "diger@example.test": "diger-fake-pass",  # an outsider, for sender/list tests
}


def _users_arg() -> str:
    # GreenMail syntax: login:password@domain  ->  login "login", address login@domain.
    # We want the login to be the full address, so give "<address>:<password>" with no domain.
    return ",".join(f"{login}:{pw}" for login, pw in USERS.items())


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


# --- java discovery ---------------------------------------------------------------------
def _java_major(java: str) -> int:
    try:
        out = subprocess.run(
            [java, "-version"], check=False, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.search(r'version "(\d+)(?:\.(\d+))?', out.stderr + out.stdout)
    if not m:
        return 0
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major


def find_java(explicit: str | None = None) -> tuple[str, int]:
    """Return (path to java executable, major version).  Picks the newest JDK it can find.

    GreenMail 2.1.x jars are Java 8 bytecode, so anything >= 8 works; newer is preferred.
    Override with --java, $GREENMAIL_JAVA or $JAVA_HOME.
    """
    exe = "java.exe" if os.name == "nt" else "java"
    cands: list[str] = []
    if explicit:
        cands.append(explicit)
    if os.environ.get("GREENMAIL_JAVA"):
        cands.append(os.environ["GREENMAIL_JAVA"])
    if os.environ.get("JAVA_HOME"):
        cands.append(str(Path(os.environ["JAVA_HOME"]) / "bin" / exe))
    if which := shutil.which("java"):
        cands.append(which)
    roots: list[Path] = []
    if os.name == "nt":
        for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
            if base:
                roots += [
                    Path(base) / "Programs" / "Eclipse Adoptium",
                    Path(base) / "Eclipse Adoptium",
                ]
                roots += [Path(base) / "Java"]
    else:
        roots += [Path("/usr/lib/jvm"), Path("/Library/Java/JavaVirtualMachines")]
    for r in roots:
        if r.is_dir():
            for d in sorted(r.iterdir()):
                for sub in ("bin", "Contents/Home/bin"):
                    cands.append(str(d / sub / exe))
    best = ("", 0)
    for c in dict.fromkeys(cands):  # de-dup, keep order
        if not Path(c).exists() and not shutil.which(c):
            continue
        major = _java_major(c)
        if explicit and c == explicit and major:
            return c, major
        if major > best[1]:
            best = (c, major)
    if best[1] < 8:
        sys.exit("error: no Java >= 8 found. Install a JDK or pass --java / set JAVA_HOME.")
    return best


def keytool_for(java: str) -> str:
    exe = "keytool.exe" if os.name == "nt" else "keytool"
    k = Path(java).parent / exe
    if k.exists():
        return str(k)
    found = shutil.which("keytool")
    if not found:
        sys.exit("error: keytool not found next to java or on PATH (need a JDK, not a bare JRE).")
    return found


# --- subcommands ------------------------------------------------------------------------
def _certs_present() -> bool:
    return KEYSTORE.exists() and CA_PEM.exists()  # CA is written last


def cmd_certs(args) -> int:
    GM_DIR.mkdir(exist_ok=True)
    force = getattr(args, "force", False)
    if _certs_present() and not force:
        print(f"certs: already present ({CA_PEM}); use --force to regenerate")
        return 0
    with _lock("certs"):
        if _certs_present() and not force:  # another process generated them meanwhile
            print(f"certs: already present ({CA_PEM})")
            return 0
        java, major = find_java(getattr(args, "java", None))
        keytool = keytool_for(java)
        tmp_ks = GM_DIR / f"keystore.{os.getpid()}.tmp"
        tmp_ca = GM_DIR / f"ca.{os.getpid()}.tmp"
        tmp_ks.unlink(missing_ok=True)
        tmp_ca.unlink(missing_ok=True)
        _run(
            [
                keytool, "-genkeypair", "-alias", "greenmail",
                "-keyalg", "RSA", "-keysize", "2048", "-sigalg", "SHA256withRSA",
                "-validity", "825", "-storetype", "PKCS12",
                "-keystore", str(tmp_ks),
                "-storepass", KEYSTORE_PASSWORD, "-keypass", KEYSTORE_PASSWORD,
                "-dname", "CN=localhost,O=posta-mcp tests",
                "-ext", "san=dns:localhost,ip:127.0.0.1",
                "-ext", "bc:c",  # self-signed leaf doubles as its own trust anchor
                "-ext", "ku=digitalSignature,keyEncipherment,keyCertSign",
                "-ext", "eku=serverAuth",
            ]
        )  # fmt: skip
        _run(
            [
                keytool, "-exportcert", "-rfc", "-alias", "greenmail",
                "-keystore", str(tmp_ks), "-storepass", KEYSTORE_PASSWORD,
                "-file", str(tmp_ca),
            ]
        )  # fmt: skip
        if os.name != "nt":
            tmp_ks.chmod(0o644)  # must be readable by the (non-root) user inside the container
        # Publish atomically; CA last so "CA exists" implies "keystore is complete".
        os.replace(tmp_ks, KEYSTORE)
        os.replace(tmp_ca, CA_PEM)
    print(f"certs: wrote {KEYSTORE} and {CA_PEM} (keytool from Java {major})")
    return 0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_jar() -> Path:
    jar = GM_DIR / JAR_NAME
    if jar.exists() and _sha256(jar) == JAR_SHA256:
        return jar
    with _lock("jar"):
        if jar.exists() and _sha256(jar) == JAR_SHA256:
            return jar
        print(f"downloading {JAR_URL}")
        tmp = jar.with_suffix(f".{os.getpid()}.part")
        with urllib.request.urlopen(JAR_URL, timeout=120) as r, tmp.open("wb") as f:
            shutil.copyfileobj(r, f)
        got = _sha256(tmp)
        if got != JAR_SHA256:
            tmp.unlink(missing_ok=True)
            sys.exit(f"error: checksum mismatch for {JAR_NAME}: got {got}, want {JAR_SHA256}")
        os.replace(tmp, jar)
    return jar


def _port_open(port: int, host: str = HOST) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _greenmail_opts(inst: Instance, keystore: str, *, docker: bool) -> list[str]:
    if docker:
        # Inside the container GreenMail always uses its default test ports; the host side of
        # the port mapping is per instance.
        ports = ["-Dgreenmail.setup.test.imaps", "-Dgreenmail.setup.test.smtps"]
        bind = ["-Dgreenmail.hostname=0.0.0.0"]
    elif inst.name:
        ports = [
            f"-Dgreenmail.imaps.port={inst.imaps_port}",
            f"-Dgreenmail.smtps.port={inst.smtps_port}",
            f"-Dgreenmail.api.port={inst.api_port}",
        ]
        bind = ["-Dgreenmail.hostname=127.0.0.1", "-Dgreenmail.api.hostname=127.0.0.1"]
    else:
        ports = ["-Dgreenmail.setup.test.imaps", "-Dgreenmail.setup.test.smtps"]  # 3993, 3465
        bind = ["-Dgreenmail.hostname=127.0.0.1", "-Dgreenmail.api.hostname=127.0.0.1"]
    return [
        *ports,
        *bind,
        f"-Dgreenmail.users={_users_arg()}",
        f"-Dgreenmail.tls.keystore.file={keystore}",
        f"-Dgreenmail.tls.keystore.password={KEYSTORE_PASSWORD}",
        f"-Dgreenmail.tls.key.password={KEYSTORE_PASSWORD}",
        "-Dgreenmail.verbose",
    ]


def _wait_ready(inst: Instance, timeout: float = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(inst.imaps_port) and _port_open(inst.smtps_port):
            return True
        time.sleep(0.5)
    return False


def _label(inst: Instance) -> str:
    return f"instance {inst.name!r}" if inst.name else "default instance"


def cmd_start(args) -> int:
    inst = get_instance(args.instance)
    in_use = _port_open(inst.imaps_port) or _port_open(inst.smtps_port)
    if in_use:
        if inst.name and not inst.pid_file.exists():
            print(
                f"error: ports {inst.imaps_port}/{inst.smtps_port} for {_label(inst)} are in use "
                "by something else (hash collision?); pick another instance name",
                file=sys.stderr,
            )
            return 1
        print(f"start: {_label(inst)} already running (see `status`)")
        return 0
    mode = args.mode or ("jar" if shutil.which("docker") is None else "docker")
    cmd_certs(args)
    if mode == "docker":
        if not shutil.which("docker"):
            sys.exit("error: --mode docker but docker is not on PATH")
        subprocess.run(["docker", "rm", "-f", inst.docker_name], check=False, capture_output=True)
        opts = " ".join(_greenmail_opts(inst, "/certs/keystore.p12", docker=True))
        _run(
            [
                "docker", "run", "-d", "--name", inst.docker_name,
                "-p", f"127.0.0.1:{inst.imaps_port}:{IMAPS_PORT}",
                "-p", f"127.0.0.1:{inst.smtps_port}:{SMTPS_PORT}",
                "-v", f"{GM_DIR}:/certs:ro",
                "-e", f"GREENMAIL_OPTS={opts}",
                DOCKER_IMAGE,
            ]
        )  # fmt: skip
        inst.pid_file.write_text(f"docker:{inst.docker_name}\n")
    else:
        java, major = find_java(args.java)
        if major < 11:
            print(
                f"warning: Java {major}: GreenMail's REST API needs Java 11+ (mail ports still "
                "work); pass --java for a newer JDK",
                file=sys.stderr,
            )
        jar = ensure_jar()
        cmd = [java, *_greenmail_opts(inst, str(KEYSTORE), docker=False), "-jar", str(jar)]
        log = inst.log_file.open("wb")
        kw: dict = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=GM_DIR, **kw
        )
        inst.pid_file.write_text(f"{proc.pid}\n")
        print(f"start: jar mode, Java {major}, pid {proc.pid}, log {inst.log_file}")
    if not _wait_ready(inst):
        print("error: GreenMail did not open its ports in time", file=sys.stderr)
        if mode == "docker":
            subprocess.run(["docker", "logs", inst.docker_name], check=False)
        elif inst.log_file.exists():
            print(inst.log_file.read_text(errors="replace")[-3000:], file=sys.stderr)
        return 1
    print(
        f"start: ready ({_label(inst)})  IMAPS {HOST}:{inst.imaps_port}  "
        f"SMTPS {HOST}:{inst.smtps_port}  CA {CA_PEM}"
    )
    return 0


def cmd_stop(args) -> int:
    inst = get_instance(args.instance)
    if not inst.pid_file.exists():
        print(f"stop: {_label(inst)} not running (no pid file)")
        return 0
    ref = inst.pid_file.read_text().strip()
    if ref.startswith("docker:"):
        subprocess.run(
            ["docker", "rm", "-f", ref.split(":", 1)[1]], check=False, capture_output=True
        )
    else:
        pid = int(ref)  # only ever the pid recorded for THIS instance
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True
            )
        else:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
    inst.pid_file.unlink(missing_ok=True)
    for _ in range(40):
        if not _port_open(inst.imaps_port) and not _port_open(inst.smtps_port):
            print(f"stop: {_label(inst)} stopped")
            return 0
        time.sleep(0.25)
    print("stop: ports still open after stop", file=sys.stderr)
    return 1


def cmd_status(args) -> int:
    inst = get_instance(args.instance)
    imaps, smtps = _port_open(inst.imaps_port), _port_open(inst.smtps_port)
    pid = inst.pid_file.read_text().strip() if inst.pid_file.exists() else "-"
    print(f"{_label(inst)}")
    print(f"IMAPS {HOST}:{inst.imaps_port} {'up' if imaps else 'down'}")
    print(f"SMTPS {HOST}:{inst.smtps_port} {'up' if smtps else 'down'}")
    print(f"pid/container: {pid}   ca: {CA_PEM if CA_PEM.exists() else 'missing (run certs)'}")
    return 0 if imaps and smtps else 1


def cmd_capabilities(args) -> int:
    import smtplib

    inst = get_instance(args.instance)
    ctx = ssl.create_default_context(cafile=str(CA_PEM))
    with (
        socket.create_connection((HOST, inst.imaps_port), timeout=10) as raw,
        ctx.wrap_socket(raw, server_hostname="localhost") as s,
    ):
        s.recv(4096)  # greeting
        s.sendall(b"a1 CAPABILITY\r\n")
        print("IMAP:", s.recv(4096).decode(errors="replace").splitlines()[0])
    with smtplib.SMTP_SSL("localhost", inst.smtps_port, context=ctx, timeout=10) as smtp:
        smtp.ehlo()
        print("SMTP:", smtp.esmtp_features)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (
        ("certs", cmd_certs),
        ("start", cmd_start),
        ("stop", cmd_stop),
        ("status", cmd_status),
        ("capabilities", cmd_capabilities),
    ):
        sp = sub.add_parser(name)
        if name in ("certs", "start"):
            sp.add_argument("--java", help="path to java executable (default: auto-detect)")
        if name == "certs":
            sp.add_argument("--force", action="store_true", help="regenerate keystore and CA")
        else:
            sp.add_argument(
                "--instance",
                default=None,
                help=f"named instance with its own ports (default: ${INSTANCE_ENV}, else default)",
            )
        if name == "start":
            sp.add_argument("--mode", choices=["jar", "docker"], help="default: jar if no docker")
        sp.set_defaults(fn=fn)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
