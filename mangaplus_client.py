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
        """Search across all title types. Returns list of {title_id, name, author, language}."""
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
                            results.append({
                                "title_id": tid,
                                "name": name,
                                "author": t.get("author", ""),
                                "language": t.get("language", "ENGLISH"),
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
        """Returns title info including chapters and next chapter release date."""
        client = self._get_client()
        detail = client.getTitleDetail(title_id=title_id)
        tdv = detail.get("titleDetailView", {})

        title_obj = tdv.get("title", {})
        title_name = title_obj.get("name", "") if isinstance(title_obj, dict) else ""

        portrait_url = ""
        if isinstance(title_obj, dict):
            portrait_url = title_obj.get("portraitImageUrl", "")

        # Next chapter release date from Unix timestamp
        next_ts = tdv.get("nextTimeStamp", 0) or 0
        next_chapter_date = ""
        if next_ts and next_ts > 0:
            try:
                dt = datetime.fromtimestamp(int(next_ts), tz=timezone.utc)
                next_chapter_date = dt.strftime("%B %d, %Y")
            except Exception:
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

        return {
            "title_name": title_name,
            "portrait_url": portrait_url,
            "next_chapter_date": next_chapter_date,
            "chapters": chapters,
        }

    def find_chapter(self, title_id: int, chapter_number: float) -> dict | None:
        info = self.get_title_info(title_id)
        for ch in info["chapters"]:
            num = ch["chapter_num"]
            if num is not None:
                try:
                    if abs(float(num) - chapter_number) < 0.01:
                        ch["title_name"] = info["title_name"]
                        ch["portrait_url"] = info.get("portrait_url", "")
                        ch["next_chapter_date"] = info.get("next_chapter_date", "")
                        return ch
                except (TypeError, ValueError):
                    pass
        return None

    def download_chapter_images(self, chapter_id: int, output_dir: str) -> list[str]:
        """Download all pages one at a time to disk. Returns sorted list of file paths."""
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

            img_bytes = _http_get(url)
            if img_bytes:
                ext = _detect_ext(img_bytes)
                path = os.path.join(output_dir, f"{img_index:04d}.{ext}")
                with open(path, "wb") as f:
                    f.write(img_bytes)
                image_paths.append(path)
                logger.info(f"Page {img_index}: {len(img_bytes)} bytes → {ext}")
                img_index += 1
                del img_bytes
                gc.collect()
            else:
                logger.warning(f"Failed to download page {img_index}")

        if not image_paths:
            raise ValueError(
                "No pages downloaded. This chapter may be subscriber-only or unavailable."
            )

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


def _http_get(url: str) -> bytes | None:
    try:
        r = requests.get(
            url,
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36",
                "Referer": "https://mangaplus.shueisha.co.jp/",
                "Origin": "https://mangaplus.shueisha.co.jp",
            },
        )
        if r.status_code == 200:
            return r.content
        logger.warning(f"HTTP {r.status_code} for {url[:80]}")
    except Exception as ex:
        logger.error(f"HTTP error: {ex}")
    return None


def _detect_ext(data: bytes) -> str:
    if data[:4] in (b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb"):
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "webp"
