"""Local export and restore of explicitly configured upstream credentials."""

import argparse
import getpass
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import tomllib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from gateway import ENV_NAME, secret


HEADER = b"WBAPI-BACKUP-1\n"
MAX_BACKUP = 2 * 1024 * 1024
MAX_CREDENTIALS = 64


def is_exportable(name):
    return isinstance(name, str) and ENV_NAME.fullmatch(name) and name != "WB_GATEWAY_TOKEN"


def declared_credentials(path):
    with open(path, "rb") as handle:
        config = tomllib.load(handle)
    rows = [config["native"]] if "native" in config else []
    rows.extend(config.get("groups", {}).values())
    selected = config.get("backup", {}).get("credentials", [])
    if not isinstance(selected, list):
        raise ValueError("backup.credentials must be a list of local credential references")
    names = [row["token_env"] for row in rows] + selected
    if any(not is_exportable(name) for name in names):
        raise ValueError("invalid upstream credential reference")
    return set(names)


def credentials_to_export(config, names, env):
    allowed = declared_credentials(config)
    if not 1 <= len(names) <= MAX_CREDENTIALS or len(names) != len(set(names)) or not set(names) <= allowed:
        raise ValueError("select distinct credentials explicitly declared in the gateway configuration")
    return {name: secret(name, env) for name in names}


def encode_credentials(entries):
    if not isinstance(entries, dict) or not 1 <= len(entries) <= MAX_CREDENTIALS:
        raise ValueError("invalid credential backup entries")
    for name, value in entries.items():
        if (not is_exportable(name) or not isinstance(value, str) or not 1 <= len(value) <= 16384
                or not value.isascii() or any(not 33 <= ord(x) <= 126 for x in value)):
            raise ValueError("invalid credential backup entry")
    return json.dumps(entries, separators=(",", ":"), sort_keys=True).encode("utf-8")


def cipher(salt, password):
    return AESGCM(Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(password))


def encrypt_backup(entries, password):
    if len(password) < 12:
        raise ValueError("use a backup passphrase of at least 12 characters")
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    return HEADER + salt + nonce + cipher(salt, password).encrypt(nonce, encode_credentials(entries), HEADER)


def decrypt_backup(raw, password):
    if not raw.startswith(HEADER) or not len(HEADER) + 44 < len(raw) <= MAX_BACKUP:
        raise ValueError("invalid backup file")
    body = raw[len(HEADER):]
    salt, nonce = body[:16], body[16:28]
    try:
        entries = json.loads(cipher(salt, password).decrypt(nonce, body[28:], HEADER))
    except (InvalidTag, ValueError, UnicodeError) as error:
        raise ValueError("wrong backup passphrase or damaged backup") from error
    encode_credentials(entries)
    return entries


def write_new(path, payload):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        path.unlink(missing_ok=True)
        raise


def restrict_windows_directory(directory):
    system = pathlib.Path(os.environ["SystemRoot"]) / "System32"
    identity = subprocess.check_output([str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"])
    sid = re.search(rb"S-1-5-(?:[0-9]+-)*[0-9]+", identity)
    if not sid:
        raise ValueError("cannot identify the current Windows user")
    principal = "*" + sid[0].decode("ascii")
    subprocess.run([str(system / "icacls.exe"), str(directory), "/inheritance:r", "/grant:r",
                    principal + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def restore_credentials(directory, entries):
    encode_credentials(entries)
    directory.mkdir(mode=0o700)
    written = []
    try:
        if os.name == "nt":
            restrict_windows_directory(directory)
        for name, value in entries.items():
            target = directory / (name + ".secret")
            write_new(target, (value + "\n").encode("ascii"))
            written.append(target)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        for path in written:
            path.unlink(missing_ok=True)
        directory.rmdir()
        raise


def passphrase(path, confirm=False):
    if path:
        with path.open("r", encoding="utf-8") as handle:
            value = handle.read(4097).rstrip("\r\n")
    else:
        if not sys.stdin.isatty():
            raise ValueError("use an interactive terminal or a local passphrase file")
        value = getpass.getpass("Backup passphrase: ")
        if confirm and not secrets.compare_digest(value.encode("utf-8"), getpass.getpass("Repeat passphrase: ").encode("utf-8")):
            raise ValueError("backup passphrases differ")
    if not 12 <= len(value) <= 4096:
        raise ValueError("backup passphrase must have between 12 and 4096 characters")
    return value.encode("utf-8")


def main():
    command = argparse.ArgumentParser(description=__doc__)
    sub = command.add_subparsers(dest="action", required=True)
    export = sub.add_parser("export")
    export.add_argument("--config", required=True, type=pathlib.Path)
    export.add_argument("--credential", action="append", required=True)
    export.add_argument("--output", required=True, type=pathlib.Path)
    restore = sub.add_parser("restore")
    restore.add_argument("--input", required=True, type=pathlib.Path)
    restore.add_argument("--directory", required=True, type=pathlib.Path)
    for parser in (export, restore):
        parser.add_argument("--passphrase-file", type=pathlib.Path)
    args = command.parse_args()
    try:
        if args.action == "export":
            entries = credentials_to_export(args.config, args.credential, os.environ)
            encrypted = encrypt_backup(entries, passphrase(args.passphrase_file, confirm=True))
            write_new(args.output, encrypted)
            print(f"Encrypted {len(entries)} configured credentials.")
        else:
            with args.input.open("rb") as handle:
                raw = handle.read(MAX_BACKUP + 1)
            entries = decrypt_backup(raw, passphrase(args.passphrase_file))
            restore_credentials(args.directory, entries)
            print(f"Restored {len(entries)} credentials into the new local directory.")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Credential backup failed: {error}") from None


if __name__ == "__main__":
    main()
