from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote


@dataclass(frozen=True)
class StoredObject:
    content: bytes
    mime: str
    sha256: str

    @classmethod
    def from_bytes(cls, content: bytes, mime: str) -> "StoredObject":
        return cls(content, mime, hashlib.sha256(content).hexdigest())


class StoragePort(Protocol):
    def upload_url(self, object_key: str, mime: str, size: int, expires_in: int) -> str: ...
    def put(self, object_key: str, content: bytes, mime: str) -> StoredObject: ...
    def get(self, object_key: str) -> StoredObject | None: ...
    def delete(self, object_key: str) -> None: ...
    def download_url(self, object_key: str, filename: str, expires_in: int) -> str: ...


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.secret = os.urandom(32)

    def upload_url(self, object_key: str, mime: str, size: int, expires_in: int) -> str:
        return self._url("upload", object_key, expires_in, f"{mime}:{size}")

    def put(self, object_key: str, content: bytes, mime: str) -> StoredObject:
        stored = StoredObject.from_bytes(content, mime)
        self.objects[object_key] = stored
        return stored

    def get(self, object_key: str) -> StoredObject | None:
        return self.objects.get(object_key)

    def delete(self, object_key: str) -> None:
        self.objects.pop(object_key, None)

    def download_url(self, object_key: str, filename: str, expires_in: int) -> str:
        return self._url("download", object_key, expires_in, filename)

    def _url(self, action: str, object_key: str, expires_in: int, scope: str) -> str:
        expires = int(time.time()) + expires_in
        message = f"{action}:{object_key}:{expires}:{scope}".encode()
        signature = hmac.new(self.secret, message, hashlib.sha256).hexdigest()
        return (
            f"/v1/storage/{action}/{quote(object_key)}?expires={expires}"
            f"&signature={signature}&scope={quote(scope)}"
        )

    def verify(
        self,
        action: str,
        object_key: str,
        expires: int,
        scope: str,
        signature: str,
    ) -> bool:
        if expires < int(time.time()):
            return False
        expected = hmac.new(
            self.secret,
            f"{action}:{object_key}:{expires}:{scope}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


class LocalStorage:
    def __init__(self, root: str | Path, signing_secret: bytes | None = None) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.secret = signing_secret or os.urandom(32)

    def upload_url(self, object_key: str, mime: str, size: int, expires_in: int) -> str:
        return self._signed("upload", object_key, expires_in, f"{mime}:{size}")

    def put(self, object_key: str, content: bytes, mime: str) -> StoredObject:
        path = self._path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.with_suffix(path.suffix + ".mime").write_text(mime, encoding="utf-8")
        return StoredObject.from_bytes(content, mime)

    def get(self, object_key: str) -> StoredObject | None:
        path = self._path(object_key)
        if not path.is_file():
            return None
        mime_path = path.with_suffix(path.suffix + ".mime")
        mime = mime_path.read_text(encoding="utf-8") if mime_path.is_file() else "application/octet-stream"
        return StoredObject.from_bytes(path.read_bytes(), mime)

    def delete(self, object_key: str) -> None:
        path = self._path(object_key)
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + ".mime").unlink(missing_ok=True)

    def download_url(self, object_key: str, filename: str, expires_in: int) -> str:
        return self._signed("download", object_key, expires_in, filename)

    def _path(self, object_key: str) -> Path:
        path = (self.root / object_key).resolve()
        if self.root not in path.parents:
            raise ValueError("Unsafe object key")
        return path

    def _signed(self, action: str, object_key: str, expires_in: int, scope: str) -> str:
        expires = int(time.time()) + expires_in
        message = f"{action}:{object_key}:{expires}:{scope}".encode()
        signature = hmac.new(self.secret, message, hashlib.sha256).hexdigest()
        return (
            f"/v1/storage/{action}/{quote(object_key)}?expires={expires}"
            f"&signature={signature}&scope={quote(scope)}"
        )

    def verify(
        self,
        action: str,
        object_key: str,
        expires: int,
        scope: str,
        signature: str,
    ) -> bool:
        if expires < int(time.time()):
            return False
        expected = hmac.new(
            self.secret,
            f"{action}:{object_key}:{expires}:{scope}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


class CosStorage:
    def __init__(
        self,
        *,
        region: str,
        bucket: str,
        secret_id: str,
        secret_key: str,
    ) -> None:
        if not all((region, bucket, secret_id, secret_key)):
            raise ValueError("COS configuration is incomplete")
        from qcloud_cos import CosConfig, CosS3Client

        self.bucket = bucket
        self.client = CosS3Client(
            CosConfig(
                Region=region,
                SecretId=secret_id,
                SecretKey=secret_key,
                Scheme="https",
            )
        )

    def upload_url(self, object_key: str, mime: str, size: int, expires_in: int) -> str:
        return self.client.get_presigned_url(
            Method="PUT",
            Bucket=self.bucket,
            Key=object_key,
            Expired=expires_in,
            Headers={"Content-Type": mime, "Content-Length": str(size)},
        )

    def put(self, object_key: str, content: bytes, mime: str) -> StoredObject:
        self.client.put_object(
            Bucket=self.bucket,
            Key=object_key,
            Body=content,
            ContentType=mime,
        )
        return StoredObject.from_bytes(content, mime)

    def get(self, object_key: str) -> StoredObject | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=object_key)
        except Exception as error:
            if getattr(error, "get_status_code", lambda: None)() == 404:
                return None
            raise
        content = response["Body"].get_raw_stream().read()
        return StoredObject.from_bytes(content, response.get("ContentType", "application/octet-stream"))

    def delete(self, object_key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=object_key)

    def download_url(self, object_key: str, filename: str, expires_in: int) -> str:
        return self.client.get_presigned_url(
            Method="GET",
            Bucket=self.bucket,
            Key=object_key,
            Expired=expires_in,
            Params={
                "response-content-disposition": f'attachment; filename="{quote(filename)}"'
            },
        )


def build_storage(settings) -> StoragePort:
    if settings.storage_backend == "memory":
        return MemoryStorage()
    if settings.storage_backend == "local":
        if (
            settings.app_env == "production"
            and len(settings.storage_signing_secret.encode()) < 32
        ):
            raise ValueError(
                "STORAGE_SIGNING_SECRET must be at least 32 bytes in production"
            )
        return LocalStorage(
            settings.storage_local_root,
            (
                settings.storage_signing_secret.encode()
                if settings.storage_signing_secret
                else None
            ),
        )
    if settings.storage_backend == "cos":
        return CosStorage(
            region=settings.cos_region,
            bucket=settings.cos_bucket,
            secret_id=settings.cos_secret_id,
            secret_key=settings.cos_secret_key,
        )
    raise ValueError("Unsupported storage backend")
