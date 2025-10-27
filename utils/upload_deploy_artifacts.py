#!/usr/bin/env python3
"""Upload ./data and .env to the remote PastPaperRank server."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path


DEFAULT_USER = "admin"
DEFAULT_HOST = "39.108.178.245"
DEFAULT_REMOTE_ROOT = "/home/admin/proj/PastPaperRank"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def run(cmd: list[str], *, dry_run: bool) -> None:
    if dry_run:
        print("[dry-run]", " ".join(shlex.quote(part) for part in cmd))
        return
    subprocess.run(cmd, check=True)


def run_ssh_command(target: str, remote_cmd: str, ssh_key: str | None, dry_run: bool) -> None:
    cmd = ["ssh"]
    if ssh_key:
        cmd += ["-i", ssh_key]
    cmd += [target, remote_cmd]
    run(cmd, dry_run=dry_run)


def ensure_remote_dirs(target: str, remote_root: str, ssh_key: str | None, dry_run: bool) -> None:
    remote_cmd = (
        f"mkdir -p {shlex.quote(remote_root)} && "
        f"mkdir -p {shlex.quote(os.path.join(remote_root, 'data'))}"
    )
    run_ssh_command(target, remote_cmd, ssh_key, dry_run)


@contextmanager
def build_data_archive(data_dir: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix="ppr-upload-") as tmpdir:
        archive_path = Path(tmpdir) / "data.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(data_dir, arcname="data")
        yield archive_path


def upload_file(local_path: Path, destination: str, ssh_key: str | None, dry_run: bool) -> None:
    cmd = ["scp"]
    if ssh_key:
        cmd += ["-i", ssh_key]
    cmd += [str(local_path), destination]
    run(cmd, dry_run=dry_run)


def upload_data(data_dir: Path, target: str, remote_root: str, ssh_key: str | None, dry_run: bool) -> None:
    archive_name = "data.tar.gz"
    remote_archive = os.path.join(remote_root, archive_name)

    if dry_run:
        print(
            "[dry-run]",
            f"tar {data_dir} -> {remote_archive} and extract on {target}",
        )
        return

    with build_data_archive(data_dir) as archive_path:
        upload_file(archive_path, f"{target}:{remote_archive}", ssh_key, dry_run=False)

        remote_cmd = (
            f"cd {shlex.quote(remote_root)} && "
            f"rm -rf data && "
            f"tar xzf {shlex.quote(archive_name)} && "
            f"rm -f {shlex.quote(archive_name)}"
        )
        run_ssh_command(target, remote_cmd, ssh_key, dry_run=False)


def upload_env(env_file: Path, target: str, remote_root: str, ssh_key: str | None, dry_run: bool) -> None:
    destination = f"{target}:{os.path.join(remote_root, '.env')}"
    upload_file(env_file, destination, ssh_key, dry_run)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload ./data and .env to the remote PastPaperRank server."
    )
    parser.add_argument("--user", default=DEFAULT_USER, help="SSH username (default: %(default)s)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="SSH host/IP (default: %(default)s)")
    parser.add_argument(
        "--remote-root",
        default=DEFAULT_REMOTE_ROOT,
        help="Remote project root (default: %(default)s)",
    )
    parser.add_argument(
        "--ssh-key",
        help="Path to the SSH private key used for authentication (optional).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands instead of executing them.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = project_root()
    data_dir = root / "data"
    env_file = root / ".env"

    missing = [path for path in (data_dir, env_file) if not path.exists()]
    if missing:
        missing_str = ", ".join(str(path) for path in missing)
        raise SystemExit(f"Missing required artifact(s): {missing_str}")

    target = f"{args.user}@{args.host}"

    ensure_remote_dirs(target, args.remote_root, args.ssh_key, args.dry_run)
    upload_data(data_dir, target, args.remote_root, args.ssh_key, args.dry_run)
    upload_env(env_file, target, args.remote_root, args.ssh_key, args.dry_run)
    print("Upload completed." if not args.dry_run else "Dry run complete.")


if __name__ == "__main__":
    main()
