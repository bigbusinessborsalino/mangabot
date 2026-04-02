import os
import zipfile
import logging
from PIL import Image

logger = logging.getLogger(__name__)


def _load_image_rgb(path: str) -> Image.Image:
    img = Image.open(path)
    if img.mode in ("RGBA", "P", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        if img.mode in ("RGBA", "LA"):
            bg.paste(img, mask=img.split()[-1])
        else:
            bg.paste(img)
        return bg
    return img.convert("RGB")


def create_pdf(image_paths: list[str], output_path: str) -> str:
    if not image_paths:
        raise ValueError("No images to convert to PDF")

    sorted_paths = sorted(image_paths)
    
    # Load ONLY the very first image into memory
    try:
        first_image = _load_image_rgb(sorted_paths[0])
    except Exception as e:
        logger.warning(f"Skipping first image {sorted_paths[0]}: {e}")
        raise ValueError("Failed to load the first image for PDF.")

    # Create a GENERATOR for the rest of the images. 
    # This loads them into RAM one-by-one exactly when the PDF needs them, preventing server crashes.
    def image_generator():
        for p in sorted_paths[1:]:
            try:
                yield _load_image_rgb(p)
            except Exception as e:
                logger.warning(f"Skipping {p}: {e}")

    # Save the PDF using the generator
    first_image.save(
        output_path,
        format="PDF",
        save_all=True,
        append_images=image_generator(),
        resolution=150,
    )
    
    logger.info(f"PDF created: {output_path} ({os.path.getsize(output_path) / 1024 / 1024:.1f} MB)")
    return output_path


def create_cbz(image_paths: list[str], output_path: str) -> str:
    if not image_paths:
        raise ValueError("No images to create CBZ")

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(image_paths):
            arcname = os.path.basename(p)
            zf.write(p, arcname)

    logger.info(f"CBZ created: {output_path} ({os.path.getsize(output_path) / 1024 / 1024:.1f} MB)")
    return output_path
