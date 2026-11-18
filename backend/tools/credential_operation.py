"""Provision and use a locally authorized, fixed credential operation."""
import argparse
import getpass
import json
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
    if url.scheme != scheme or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("invalid target")
    if kind == "git" and not re.fullmatch(r"/[A-Za-z0-9_./-]+", url.path):
        raise ValueError("expected repository path")
    if kind == "postgres" and not re.fullmatch(r"/[A-Za-z0-9_-]+", url.path):
        raise ValueError("expected database name")
    return url


def use(record):
    kind, target, user, secret = record
    url = validate(kind, target)
    if kind == "postgres":
        import psycopg
        with psycopg.connect(host=url.hostname, port=url.port or 5432, dbname=url.path[1:],
                             user=user, password=secret, sslmode="verify-full", connect_timeout=10) as db:
            return db.execute("SELECT current_database()").fetchone()[0]
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
        return response.status


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
        main()
    except Exception:
        sys.exit("Credential operation failed")
