import gc
import os
import re
import logging
import requests
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DEVICE_ID = os.getenv("MANGAPLUS_DEVICE_ID", "550e8400-e29b-41d4-a716-446655440001")

class MangaPlusClient:
    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            from mangaplus import MangaPlus
            from mangaplus.constants import Language, Viewer
            self._client = MangaPlus(lang=Language.ENGLISH, viewer=Viewer.VERTICAL)
            try:
                self._client.register(device_id=DEVICE_ID)
                logger.info("MangaPlus device registered")
            except Exception as ex:
                logger.warning(f"Device registration warning (continuing): {ex}")
        return self._client

    def search_manga(self, query: str) -> list[dict]:
        from mangaplus.constants import TitleType
        client = self._get_client()
        query_lower = query.lower().strip()
        results = []
        seen_ids = set()

        for title_type in [TitleType.SERIALIZING, TitleType.COMPLETED, TitleType.ONE_SHOT]:
            try:
                resp = client.getAllTitlesV3(title_type=title_type)
                groups = resp.get("searchView", {}).get("allTitlesGroup", [])
                for group in groups:
                    for t in group.get("titles", []):
                        name = t.get("name", "")
                        tid = t.get("titleId")
                        if not name or tid is None or tid in seen_ids:
                            continue
                        if query_lower in name.lower():
                            seen_ids.add(tid)
                            lang = t.get("language", "ENGLISH")
                            results.append({
                                "title_id": tid,
                                "name": name,
                                "author": t.get("author", ""),
                                "language": lang,
                            })
            except Exception as ex:
                logger.error(f"Error fetching {title_type}: {ex}")

        def sort_key(r):
            is_english = 0 if r["language"] == "ENGLISH" else 1
            is_exact = 0 if r["name"].lower() == query_lower else 1
            return (is_english, is_exact, r["name"].lower())

        results.sort(key=sort_key)
        return results[:15]

    def get_title_info(self, title_id: int) -> dict:
        client = self._get_client()
        detail = client.getTitleDetail(title_id=title_id)
        tdv = detail.get("titleDetailView", {})

        title_obj = tdv.get("title", {})
        title_name = title_obj.get("name", "") if isinstance(title_obj, dict) else ""

        portrait_url = ""
        if isinstance(title_obj, dict):
            portrait_url = title_obj.get("portraitImageUrl", "")
            
            # --- THE BUG FIX FOR THE TypeError ---
            next_ts = title_obj.get("nextTimeStamp")
            try:
                if next_ts and int(next_ts) > 0:
                    pass # Handled safely
            except (ValueError, TypeError):
                pass

        raw_chapters = tdv.get("chapterListV2", [])
        chapters = []
        for ch in raw_chapters:
            cid = ch.get("chapterId")
            name = ch.get("name", "")         
            subtitle = ch.get("subTitle", "") 
            num = _parse_chapter_number(name) or _parse_chapter_number(subtitle)
            if cid is not None:
                chapters.append({
                    "chapter_id": cid,
                    "chapter_num": num,
                    "sub_title": subtitle,
                    "name": name,
                    "thumbnail_url": ch.get("thumbnailUrl", ""),
                })

        return {"title_name": title_name, "portrait_url": portrait_url, "chapters": chapters}

    def find_chapter(self, title_id: int, chapter_number: float) -> dict | None:
        info = self.get_title_info(title_id)
        for ch in info["chapters"]:
            num = ch["chapter_num"]
            if num is not None:
                try:
                    if abs(float(num) - chapter_number) < 0.01:
                        ch["title_name"] = info["title_name"]
                        ch["portrait_url"] = info.get("portrait_url", "")
                        return ch
                except (TypeError, ValueError):
                    pass
        return None

    def download_chapter_images(self, chapter_id: int, output_dir: str) -> list[str]:
        """Download directly to disk in chunks to bypass RAM limits."""
        from mangaplus.constants import Quality
        client = self._get_client()
        data = client.getMangaData(chapter_id=chapter_id, quality=Quality.SUPER_HIGH)

        mv = data.get("mangaViewer", {})
        raw_pages = mv.get("pages", [])

        logger.info(f"Chapter {chapter_id}: {len(raw_pages)} page entries")

        image_paths = []
        img_index = 1

        for page in raw_pages:
            mp = page.get("mangaPage")
            if not mp:
                continue  
            url = mp.get("imageUrl")
            if not url:
                continue

            temp_path = os.path.join(output_dir, f"temp_{img_index:04d}")
            
            # STREAM DIRECTLY TO DISK (RAM Saver)
            try:
                with requests.get(
                    url, stream=True, timeout=30,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Linux; Android 12; Pixel 6)",
                        "Referer": "https://mangaplus.shueisha.co.jp/",
                    }
                ) as r:
                    if r.status_code == 200:
                        with open(temp_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
            except Exception as ex:
                logger.error(f"Download stream error: {ex}")
                continue

            if os.path.exists(temp_path):
                # Detect extension from downloaded file
                with open(temp_path, "rb") as f:
                    header = f.read(8)
                
                ext = "webp"
                if header[:4] in (b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb"):
                    ext = "jpg"
                elif header[:8] == b"\x89PNG\r\n\x1a\n":
                    ext = "png"
                elif header[:6] in (b"GIF87a", b"GIF89a"):
                    ext = "gif"

                final_path = os.path.join(output_dir, f"{img_index:04d}.{ext}")
                os.rename(temp_path, final_path)
                image_paths.append(final_path)
                img_index += 1

        if not image_paths:
            raise ValueError("No pages downloaded. This chapter may be subscriber-only.")

        return sorted(image_paths)


def _parse_chapter_number(s: str) -> float | None:
    if not s:
        return None
    m = re.search(r"#(\d+(?:\.\d+)?)", s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m = re.search(r"[Cc]hapter\s+(\d+(?:\.\d+)?)", s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m = re.match(r"^(\d+(?:\.\d+)?)$", s.strip())
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None
