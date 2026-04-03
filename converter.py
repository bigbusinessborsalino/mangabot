import gc
import os
import re
import zipfile
import logging
import tempfile

import img2pdf
from PIL import Image

logger = logging.getLogger(__name__)

def _to_jpeg(src_path: str, dst_path: str) -> None:
    """Convert any image (WebP/PNG/etc.) to JPEG, releasing memory immediately."""
    with Image.open(src_path) as img:
        if img.mode in ("RGBA", "P", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            rgb = bg
        else:
            rgb = img.convert("RGB")
        rgb.save(dst_path, "JPEG", quality=85, optimize=True, progressive=True)

def create_pdf(image_paths: list[str], output_path: str) -> str:
    if not image_paths:
        raise ValueError("No images to convert to PDF")

    jpeg_paths: list[str] = []
    tmp_dir = os.path.dirname(output_path)

    for i, src in enumerate(sorted(image_paths)):
        dst = os.path.join(tmp_dir, f"_conv_{i:04d}.jpg")
        try:
            _to_jpeg(src, dst)
            jpeg_paths.append(dst)
        except Exception as ex:
            logger.warning(f"Skipping page {i} ({src}): {ex}")
        gc.collect()

    if not jpeg_paths:
        raise ValueError("No valid images could be converted for PDF")

    try:
        # Build PDF in memory, write it, then force delete it
        pdf_bytes = img2pdf.convert(jpeg_paths)
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)
            
        del pdf_bytes  # <--- CRITICAL RAM SAVER
        gc.collect()
        
    finally:
        for p in jpeg_paths:
            try:
                os.remove(p)
            except OSError:
                pass
        gc.collect()

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    logger.info(f"PDF created: {output_path} ({size_mb:.1f} MB, {len(jpeg_paths)} pages)")
    return output_path


def create_cbz(image_paths: list[str], output_path: str) -> str:
    """CBZ is just a zip of images — no decoding needed, already low memory."""
    if not image_paths:
        raise ValueError("No images to create CBZ")

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(image_paths):
            z.write(p, arcname=os.path.basename(p))

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    logger.info(f"CBZ created: {output_path} ({size_mb:.1f} MB, {len(image_paths)} pages)")
    return output_path
