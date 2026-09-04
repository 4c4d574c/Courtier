#!/usr/bin/env python3
"""Provision the plugin I/O transfer bucket on MinIO (idempotent).

Creates, against the cluster addressed by MINIO_* env / .env:

1. The transfer bucket (default ``courtier-plugin-io``) with a 24h
   lifecycle rule — in-flight plugin files are transient by design.
2. An IAM policy (``plugin-io-rw``) allowing Get/Put/Delete on that
   bucket and nothing else.
3. A restricted user for plugin processes
   (``COURTIER_PLUGIN_MINIO_USER``, default ``courtier-plugins``) with
   that policy attached.  Plugin deployments set MINIO_ENDPOINT/
   MINIO_ACCESS_KEY/MINIO_SECRET_KEY to this account.

Usage:
    uv run scripts/minio_plugin_io.py            # provision (idempotent)
    uv run scripts/minio_plugin_io.py --verify   # also verify bucket isolation

Rotation: change COURTIER_PLUGIN_MINIO_SECRET and re-run (user_add
overwrites the secret), then update plugin environments and restart
plugin services.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

POLICY_NAME = "plugin-io-rw"
DEFAULT_BUCKET = "courtier-plugin-io"
DEFAULT_USER = "courtier-plugins"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _load_dotenv_settings() -> None:
    """Pull .env values into os.environ for keys we consume (if unset)."""
    from pathlib import Path

    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if (
                key.startswith("MINIO_") or key.startswith("COURTIER_PLUGIN_MINIO_")
            ) and not os.environ.get(key):
                os.environ[key] = value.strip().strip('"').strip("'")
        break


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="verify bucket isolation")
    args = parser.parse_args()

    _load_dotenv_settings()

    endpoint = _env("MINIO_ENDPOINT", "localhost:9000")
    secure = _env("MINIO_SECURE").lower() in ("1", "true", "yes", "on")
    # Admin ops need the root account; fall back to the app account.
    admin_key = _env("MINIO_ROOT_USER") or _env("MINIO_ACCESS_KEY")
    admin_secret = _env("MINIO_ROOT_PASSWORD") or _env("MINIO_SECRET_KEY")
    bucket = _env("MINIO_BUCKET_PLUGIN_IO", DEFAULT_BUCKET)
    plugin_user = _env("COURTIER_PLUGIN_MINIO_USER", DEFAULT_USER)
    plugin_secret = _env("COURTIER_PLUGIN_MINIO_SECRET")

    if not admin_key or not admin_secret:
        print("error: MINIO_ROOT_USER/MINIO_ROOT_PASSWORD (or MINIO_ACCESS_KEY/SECRET) required")
        return 1
    if not plugin_secret:
        print("error: COURTIER_PLUGIN_MINIO_SECRET required (choose a strong secret)")
        return 1

    from minio import Minio, MinioAdmin
    from minio.commonconfig import Filter
    from minio.credentials import StaticProvider
    from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule

    client = Minio(endpoint, access_key=admin_key, secret_key=admin_secret, secure=secure)
    admin = MinioAdmin(
        endpoint=endpoint, credentials=StaticProvider(admin_key, admin_secret), secure=secure
    )

    # 1. Bucket + lifecycle ------------------------------------------------
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
        print(f"bucket created: {bucket}")
    else:
        print(f"bucket exists:  {bucket}")

    rule = Rule(
        status="Enabled",
        rule_filter=Filter(prefix=""),
        rule_id="expire-24h",
        expiration=Expiration(days=1),
    )
    client.set_bucket_lifecycle(bucket, LifecycleConfig([rule]))
    print(f"lifecycle set:  {bucket} expires objects after 24h")

    # 2. Bucket-scoped policy ----------------------------------------------
    policy_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:AbortMultipartUpload",
                    "s3:ListMultipartUploadParts",
                ],
                "Resource": [f"arn:aws:s3:::{bucket}/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
        ],
    }
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="plugin-io-policy-", delete=False
    ) as pf:
        json.dump(policy_doc, pf)
        policy_path = pf.name
    try:
        admin.policy_add(POLICY_NAME, policy_path)
    finally:
        os.unlink(policy_path)
    print(f"policy set:     {POLICY_NAME} (rw on {bucket} only)")

    # 3. Restricted user -----------------------------------------------------
    admin.user_add(plugin_user, plugin_secret)
    admin.policy_set(POLICY_NAME, user=plugin_user)
    print(f"user ready:     {plugin_user} (policy {POLICY_NAME} attached)")

    if args.verify:
        plugin_client = Minio(
            endpoint, access_key=plugin_user, secret_key=plugin_secret, secure=secure
        )
        plugin_client.put_object(
            bucket, "provision-check.txt", __import__("io").BytesIO(b"ok"), length=2
        )
        print(f"verify:         {plugin_user} can write {bucket} ✓")
        try:
            # list_objects is lazy — force iteration to trigger the API call.
            list(plugin_client.list_objects("courtier-docs"))
            print("verify:         FAIL — restricted user listed courtier-docs!")
            return 1
        except Exception:
            print("verify:         courtier-docs access denied as expected ✓")

    print("done. Plugin env: MINIO_ACCESS_KEY=" + plugin_user)
    return 0


if __name__ == "__main__":
    sys.exit(main())
