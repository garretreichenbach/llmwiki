from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from html_parser import Image, Parser


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
IMAGE_TIMEOUT = 10
IMAGE_CONCURRENCY = 6

SAFE_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/avif": "avif",
}


@dataclass
class WebclipAsset:
    filename: str
    src: str
    data: bytes
    content_type: str
    file_type: str
    original_url: str
    alt: str
    sha256: str
    index: int
    document_id: str | None = None

    @property
    def markdown_src(self) -> str:
        return f"./{self.src}"

    def metadata(self) -> dict:
        return {
            "src": self.markdown_src,
            "path": self.src,
            "filename": self.filename,
            "content_type": self.content_type,
            "file_type": self.file_type,
            "original_url": self.original_url,
            "alt": self.alt,
            "sha256": self.sha256,
            "index": self.index,
            "document_id": self.document_id,
        }


async def materialize_webclip_assets(
    markdown: str,
    images: list[Image],
    asset_dir_name: str,
) -> tuple[str, list[WebclipAsset]]:
    if not images:
        return markdown, []

    sem = asyncio.Semaphore(IMAGE_CONCURRENCY)
    total_bytes = 0
    assets_by_ref: dict[str, WebclipAsset] = {}

    async def fetch_one(index: int, image: Image) -> None:
        nonlocal total_bytes
        if not image.ref:
            return
        async with sem:
            fetched = await _fetch_image(image.url)
        if not fetched:
            return

        data, content_type = fetched
        if total_bytes + len(data) > MAX_TOTAL_BYTES:
            return
        total_bytes += len(data)

        ext = SAFE_MIME_EXT.get(content_type) or _guess_extension(image.url) or "bin"
        filename = f"image-{index:02d}.{ext}"
        src = f"{asset_dir_name}/{filename}"
        assets_by_ref[image.ref] = WebclipAsset(
            filename=filename,
            src=src,
            data=data,
            content_type=content_type,
            file_type=ext,
            original_url=image.url,
            alt=image.alt,
            sha256=hashlib.sha256(data).hexdigest(),
            index=index,
        )

    await asyncio.gather(*(fetch_one(i, image) for i, image in enumerate(images, start=1)))

    for image in images:
        token = f"llmwiki-image://{image.ref}"
        asset = assets_by_ref.get(image.ref)
        markdown = markdown.replace(token, asset.markdown_src if asset else image.url)

    assets = [assets_by_ref[image.ref] for image in images if image.ref in assets_by_ref]
    return markdown, assets


async def _fetch_image(url: str) -> tuple[bytes, str] | None:
    if url.startswith("data:"):
        return _decode_data_image(url)

    resolved = await asyncio.to_thread(Parser._resolve_safe, url)
    if not resolved:
        return None

    safe_ip, host, port, scheme, path = resolved
    ip_str = f"[{safe_ip}]" if ":" in safe_ip else safe_ip
    default_port = 443 if scheme == "https" else 80
    port_suffix = f":{port}" if port != default_port else ""
    pinned_url = f"{scheme}://{ip_str}{port_suffix}{path}"

    try:
        async with httpx.AsyncClient(follow_redirects=False, verify=False) as client:
            resp = await client.get(
                pinned_url,
                headers={"Host": host, "User-Agent": "Mozilla/5.0"},
                timeout=IMAGE_TIMEOUT,
            )
            resp.raise_for_status()
    except Exception:
        return None

    data = resp.content
    if len(data) > MAX_IMAGE_BYTES:
        return None

    content_type = _clean_content_type(resp.headers.get("content-type", ""))
    if content_type not in SAFE_MIME_EXT:
        content_type = _guess_content_type(url)
    if content_type not in SAFE_MIME_EXT:
        return None

    return data, content_type


def _decode_data_image(url: str) -> tuple[bytes, str] | None:
    match = re.match(r"^data:([^;,]+)(;base64)?,(.*)$", url, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    content_type = _clean_content_type(match.group(1))
    if content_type not in SAFE_MIME_EXT:
        return None
    try:
        payload = match.group(3)
        data = base64.b64decode(payload, validate=False) if match.group(2) else payload.encode("utf-8")
    except Exception:
        return None
    if len(data) > MAX_IMAGE_BYTES:
        return None
    return data, content_type


def _clean_content_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _guess_content_type(url: str) -> str:
    guessed, _ = mimetypes.guess_type(urlparse(url).path)
    return _clean_content_type(guessed or "")


def _guess_extension(url: str) -> str | None:
    content_type = _guess_content_type(url)
    if content_type in SAFE_MIME_EXT:
        return SAFE_MIME_EXT[content_type]
    suffix = urlparse(url).path.rsplit(".", 1)[-1].lower()
    return suffix if suffix in {"jpg", "jpeg", "png", "gif", "webp", "avif"} else None
