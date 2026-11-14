"""Where big files live: Cloudflare R2 (S3-compatible) when it's configured.

Uploads go from the browser straight to R2 through a one-time signed URL that the server
only hands out after checking the access code, so a wrong code never sends a byte and
the server's disk never holds an upload. The worker downloads a lecture while it works
on it and deletes the local copy afterwards. Web copies and summary videos are uploaded
to R2 as soon as they're made, and pages play them through short-lived signed URLs (the
bucket stays private).

Without R2 settings, `storage_from(settings)` is None and everything stays on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from engram.errors import ProviderError

if TYPE_CHECKING:
    from engram.config import Settings

URL_SECONDS = 6 * 3600  # a signed playback link lasts a long lecture with pauses


class Storage(Protocol):
    origin: str  # what the Content-Security-Policy must allow for media and uploads

    def upload_url(self, key: str, size: int, content_type: str) -> str: ...
    def url(self, key: str, *, download: str | None = None) -> str: ...
    def head(self, key: str) -> dict[str, Any] | None: ...
    def put_file(self, path: Path, key: str, content_type: str) -> None: ...
    def get_file(self, key: str, path: Path) -> None: ...
    def delete(self, key: str) -> None: ...


class R2Storage:
    def __init__(
        self, account_id: str, access_key: str, secret_key: str, bucket: str, *,
        client: Any | None = None,
    ) -> None:  # fmt: skip
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.origin = f"https://{account_id}.r2.cloudflarestorage.com"
        self.client = client or boto3.client(
            "s3", endpoint_url=self.origin, aws_access_key_id=access_key,
            aws_secret_access_key=secret_key, region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 5}),
        )  # fmt: skip

    def upload_url(self, key: str, size: int, content_type: str) -> str:
        """A signed PUT for exactly this key, size and type, valid for an hour. The size is
        part of the signature, so the browser can't send a bigger file than it declared."""
        url: str = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentLength": size,
                "ContentType": content_type,
            },  # fmt: skip
            ExpiresIn=3600,
        )
        return url

    def url(self, key: str, *, download: str | None = None) -> str:
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if download:
            params["ResponseContentDisposition"] = f'attachment; filename="{download}"'
        url: str = self.client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=URL_SECONDS
        )
        return url

    def head(self, key: str) -> dict[str, Any] | None:
        from botocore.exceptions import ClientError

        try:
            r = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise ProviderError(f"R2 couldn't be reached: {e}") from e
        return {"size": int(r["ContentLength"]), "etag": str(r.get("ETag", "")).strip('"')}

    def put_file(self, path: Path, key: str, content_type: str) -> None:
        self.client.upload_file(str(path), self.bucket, key,
                                ExtraArgs={"ContentType": content_type})  # fmt: skip

    def get_file(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.part")
        self.client.download_file(self.bucket, key, str(tmp))
        tmp.replace(path)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def allow_browser_uploads(self, origins: list[str]) -> None:
        """Bucket CORS so pages on these origins may PUT uploads (needs an admin token)."""
        self.client.put_bucket_cors(
            Bucket=self.bucket,
            CORSConfiguration={"CORSRules": [{
                "AllowedOrigins": origins, "AllowedMethods": ["PUT", "GET", "HEAD"],
                "AllowedHeaders": ["content-type"], "MaxAgeSeconds": 3600,
            }]},
        )  # fmt: skip


def storage_from(settings: Settings) -> Storage | None:
    if not settings.r2_enabled:
        return None
    assert settings.r2_account_id and settings.r2_access_key_id
    assert settings.r2_secret_access_key and settings.r2_bucket
    return R2Storage(settings.r2_account_id, settings.r2_access_key_id,
                     settings.r2_secret_access_key, settings.r2_bucket)  # fmt: skip
