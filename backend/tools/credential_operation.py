"""Provision and use a locally authorized, fixed credential operation."""
import argparse
import getpass
import json
import os
import secrets
import re
import sys
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener, Request


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def validate(kind, target):
    if any(ord(c) < 33 or c in "\\\x7f" for c in target):
        raise ValueError("invalid target")
    url = urlsplit(target)
    scheme = "postgresql" if kind == "postgres" else "https"
    if not url.port and url.port is not None:
        raise ValueError("invalid port")
    if url.scheme != scheme or not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment:
        raise ValueError("invalid target")
    if kind in ("git", "clone") and not re.fullmatch(r"/[A-Za-z0-9_./-]+", url.path):
        raise ValueError("expected repository path")
    if kind == "postgres" and not re.fullmatch(r"/[A-Za-z0-9_-]+", url.path):
        raise ValueError("expected database name")
    return url


def use(record, workspace=None):
    kind, target, user, secret, *options = record
    url = validate(kind, target)
    if kind == "postgres":
        import psycopg
        with psycopg.connect(host=url.hostname, port=url.port or 5432, dbname=url.path[1:],
                             user=user, password=secret, sslmode="verify-full", connect_timeout=10) as db:
            if not options or not options[0]:
                return db.execute("SELECT current_database()").fetchone()[0]
            db.execute("SET TRANSACTION READ ONLY")
            db.execute("SET LOCAL statement_timeout = '10s'")
            with db.cursor(name="authorized_query") as cur:
                cur.execute(options[0])
                return [[str(cell)[:256] for cell in row[:12]] for row in cur.fetchmany(10)]
    if kind == "clone":
        import base64
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path
        if not workspace or not Path(workspace).is_dir():
            raise ValueError("workspace required")
        # Fresh destination and empty configuration prevent local repo hooks,
        # helpers or rewrites from seeing the authorization header.
        with tempfile.TemporaryDirectory() as isolated:
            env = {k: v for k, v in os.environ.items()
                   if not k.upper().startswith(("GIT_", "XDG_", "LD_", "DYLD_"))}
            env.update(HOME=isolated, USERPROFILE=isolated, GIT_CONFIG_NOSYSTEM="1",
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                       GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                       GIT_CONFIG_VALUE_0="Authorization: Basic " + base64.b64encode(
                           (user + ":" + secret).encode()).decode())
            if os.environ.get("SSL_CERT_FILE"):
                env["GIT_SSL_CAINFO"] = os.environ["SSL_CERT_FILE"]
                if sys.platform == "win32":
                    env.update(GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_1="http.sslBackend",
                               GIT_CONFIG_VALUE_1="openssl")
            search = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                     if os.path.isabs(p))
            exe = shutil.which("git", path=search)
            if not exe or not os.path.isabs(exe):
                raise ValueError("Git unavailable")
            dest = tempfile.mkdtemp(prefix="repo-", dir=os.path.abspath(workspace))
            try:
                subprocess.run([exe, "-c", "http.followRedirects=false", "-c", "http.sslVerify=true",
                                "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                                "clone", "--template=", "--", target, dest],
                               env=env, cwd=isolated, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=100, check=True)
                return os.path.basename(dest)
            except BaseException:
                shutil.rmtree(dest, ignore_errors=True)
                raise
    headers = {}
    if kind == "git":
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode((user + ":" + secret).encode()).decode()
        target = target.rstrip("/") + "/info/refs?service=git-upload-pack"
    else:
        headers["Authorization"] = "Bearer " + secret
    with build_opener(ProxyHandler({}), NoRedirect()).open(Request(target, headers=headers), timeout=15) as response:
        if kind == "git" and response.headers.get_content_type() != "application/x-git-upload-pack-advertisement":
            raise ValueError("unexpected Git response")
        if kind == "api":
            data = response.read(2049)
            return {"status": response.status, "text": data[:2048].decode("utf-8","replace"),
                    "truncated": len(data)>2048}
        if kind == "git":
            advertisement=response.read(32768)
            refs=[]
            while len(advertisement)>=4:
                size=int(advertisement[:4],16)
                if size==0:
                    advertisement=advertisement[4:]
                    continue
                if size<4 or size>len(advertisement):
                    break
                line=advertisement[4:size].split(b"\0",1)[0].strip()
                advertisement=advertisement[size:]
                if re.fullmatch(rb"[0-9a-f]{40,64} (HEAD|refs/[^\s]+)",line):
                    refs.append(line.decode("utf-8","replace"))
                if len(refs)>=32:
                    break
            return refs
        return response.status


def broker(request):
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring as Backend
    elif sys.platform == "darwin":
        from keyring.backends.macOS import Keyring as Backend
    else:
        from keyring.backends.SecretService import Keyring as Backend
    vault = Backend()
    session = request["session"]
    if not session or len(session) > 256:
        raise ValueError("invalid session")
    import hashlib
    scope = hashlib.sha256(json.dumps([os.path.realpath(request["root"]), session]).encode()).hexdigest()
    namespace = "light-agent/managed/" + scope
    action = request["action"]
    if action == "grant":
        kind, target, user, secret = (request.get(k, "") for k in ("kind", "target", "user", "secret"))
        if kind not in ("git", "clone", "postgres", "api") or not secret or len(secret.encode()) > 1024:
            raise ValueError("invalid grant")
        validate(kind, target)
        if (kind in ("git", "clone", "postgres") and not user) or (kind in ("git", "clone") and ":" in user):
            raise ValueError("invalid username")
        if any(ord(c) < 32 or ord(c) == 127 for c in user + secret):
            raise ValueError("invalid credential")
        sql = request.get("query", "")
        if sql and (kind!="postgres" or len(sql)>1024 or ";" in sql or not re.match(r"(?is)^\s*select\s",sql)):
            raise ValueError("expected a single authorized SELECT")
        ref = "cred_" + secrets.token_hex(16)
        vault.set_password(namespace, ref, json.dumps([kind, target, user, secret, sql]))
        return {"reference": ref}
    ref = request["reference"]
    if not re.fullmatch(r"cred_[0-9a-f]{32}", ref):
        raise ValueError("invalid reference")
    if action == "revoke":
        vault.delete_password(namespace, ref)
        return {"revoked": True}
    if action != "use":
        raise ValueError("unknown action")
    raw = vault.get_password(namespace, ref)
    if not raw:
        raise ValueError("unknown grant")
    record = json.loads(raw)
    result = use(record, request.get("workspace"))
    # Never forward a server echo of the authorization value.
    import base64
    from urllib.parse import quote
    sensitive = (record[3], quote(record[3],safe=""), base64.b64encode(
        (record[2]+":"+record[3]).encode()).decode())
    def scrub(value):
        if isinstance(value,str):
            for key in sensitive:
                value=value.replace(key,"[REDACTED]")
        elif isinstance(value,list):
            value=[scrub(item) for item in value]
        elif isinstance(value,dict):
            value={key:scrub(item) for key,item in value.items()}
        return value
    return {"operation": record[0], "result": scrub(result)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["grant", "use", "revoke"])
    parser.add_argument("reference")
    parser.add_argument("--kind", choices=["git", "postgres", "api"])
    parser.add_argument("--target")
    parser.add_argument("--user", default="")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.reference):
        parser.error("invalid reference")
    if sys.platform != "win32":
        parser.error("Windows Credential Manager required")
    from keyring.backends.Windows import WinVaultKeyring
    vault = WinVaultKeyring()
    service = "light-agent/strict/" + args.reference
    if args.action == "grant":
        if not args.kind or not args.target or not sys.stdin.isatty():
            parser.error("grant requires an interactive terminal, kind and target")
        validate(args.kind, args.target)
        secret = getpass.getpass("Credential: ")
        if not secret or any(ord(c) < 32 for c in secret):
            parser.error("invalid credential")
        vault.set_password(service, "grant", json.dumps([args.kind, args.target, args.user, secret]))
    elif args.action == "revoke":
        vault.delete_password(service, "grant")
    else:
        print(use(json.loads(vault.get_password(service, "grant"))))


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--broker"]:
            print(json.dumps(broker(json.loads(sys.stdin.buffer.readline(16384)))))
        else:
            main()
    except Exception:
        sys.exit("Credential operation failed")
