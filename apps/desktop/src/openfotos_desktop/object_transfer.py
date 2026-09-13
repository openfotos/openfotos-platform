"""Exact-byte transfer mechanics for private object-store URLs."""

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

import httpx

_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class UploadResult:
    status_code: int
    sha256: str
    content_md5: str


@dataclass(frozen=True)
class DownloadResult:
    status_code: int
    sha256: str


class _DigestingBody:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sha256 = hashlib.sha256()
        self.md5 = hashlib.md5(usedforsecurity=False)

    def __iter__(self):
        with self.path.open("rb") as source:
            while chunk := source.read(_CHUNK_BYTES):
                self.sha256.update(chunk)
                self.md5.update(chunk)
                yield chunk


class ObjectTransferClient:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=httpx.Timeout(600, connect=15))
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def put_file(self, *, url: str, headers: dict, path: Path) -> UploadResult:
        body = _DigestingBody(path)
        response = self.client.put(url, headers=headers, content=body)
        return UploadResult(
            status_code=response.status_code,
            sha256=body.sha256.hexdigest(),
            content_md5=base64.b64encode(body.md5.digest()).decode(),
        )

    def download_file(self, *, url: str, destination: Path) -> DownloadResult:
        digest = hashlib.sha256()
        with self.client.stream("GET", url) as response:
            if response.status_code == 200:
                with destination.open("wb") as output:
                    for chunk in response.iter_bytes(_CHUNK_BYTES):
                        digest.update(chunk)
                        output.write(chunk)
            return DownloadResult(
                status_code=response.status_code,
                sha256=digest.hexdigest(),
            )

    def get(self, url: str) -> httpx.Response:
        return self.client.get(url)


def path_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
