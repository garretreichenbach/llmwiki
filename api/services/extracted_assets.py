from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass


PDF_IMAGE_MIME = {
    "jpg": "image/jpeg",
    "png": "image/png",
}


@dataclass
class ExtractedAsset:
    document_id: str
    filename: str
    path: str
    src: str
    data: bytes
    content_type: str
    file_type: str
    parent_document_id: str
    page: int
    index: int
    kind: str
    original_id: str | None = None

    @property
    def markdown_src(self) -> str:
        return f"./{self.src}"

    def metadata(self) -> dict:
        return {
            "asset": True,
            "hidden": True,
            "kind": self.kind,
            "parent_document_id": self.parent_document_id,
            "document_id": self.document_id,
            "src": self.markdown_src,
            "path": self.src,
            "filename": self.filename,
            "content_type": self.content_type,
            "file_type": self.file_type,
            "sha256": hashlib.sha256(self.data).hexdigest(),
            "page": self.page,
            "index": self.index,
            "original_id": self.original_id,
        }


def build_pdf_image_assets(
    parent_document_id: str,
    parent_filename: str,
    parent_path: str,
    pages_with_images: list[tuple[int, str, list[dict]]],
) -> tuple[list[ExtractedAsset], dict[int, dict]]:
    stem = _stem(parent_filename)
    asset_dir = f"{stem}.assets"
    asset_path = f"{parent_path.rstrip('/')}/{asset_dir}/"
    if not asset_path.startswith("/"):
        asset_path = f"/{asset_path}"

    assets: list[ExtractedAsset] = []
    page_elements: dict[int, dict] = {}

    for page_num, _, images in pages_with_images:
        if not images:
            continue

        page_imgs = []
        for index, image in enumerate(images, start=1):
            file_type = _normalize_image_type(str(image.get("format") or "png"))
            filename = f"page-{page_num:03d}-image-{index:02d}.{file_type}"
            src = f"{asset_dir}/{filename}"
            asset = ExtractedAsset(
                document_id=str(uuid.uuid4()),
                filename=filename,
                path=asset_path,
                src=src,
                data=image["bytes"],
                content_type=PDF_IMAGE_MIME[file_type],
                file_type=file_type,
                parent_document_id=parent_document_id,
                page=page_num,
                index=index,
                kind="pdf_image",
                original_id=image.get("id"),
            )
            assets.append(asset)
            page_imgs.append({
                "id": asset.document_id,
                "src": asset.markdown_src,
                "filename": asset.filename,
            })

        page_elements[page_num] = {"images": page_imgs}

    return assets, page_elements


def _stem(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = re.sub(r"[^\w\s\-.]", "", stem.lower().replace(" ", "-"))[:80].strip("-._")
    return stem or "document"


def _normalize_image_type(value: str) -> str:
    value = value.lower().lstrip(".")
    if value in {"jpg", "jpeg"}:
        return "jpg"
    if value == "png":
        return "png"
    return "png"
