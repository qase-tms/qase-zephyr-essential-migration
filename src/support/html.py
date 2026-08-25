"""HTML helpers shared by the case and run/result importers.

Zephyr Essential rich-text fields (test-case objective/precondition, execution
comments) arrive as HTML with inline ``<img>`` tags pointing at the session-
gated CDN. These helpers convert that HTML to Qase-friendly markdown, embedding
inline images that were uploaded to Qase and preserving the filename otherwise.
"""
import re

_IMG_TAG_RE = re.compile(r"<img[^>]*>", re.IGNORECASE)
_SRC_RE = re.compile(r'src=["\']([^"\']+)["\']')
_IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
_CDN_NAME_RE = re.compile(r'^[a-f0-9-]+-\d+-(.+)$')


def strip_html(text: str, image_map: dict = None) -> str:
    """Strip HTML tags and decode common entities.

    ``<img>`` tags become an inline markdown image ``![name](qase_url)`` when
    the source URL is found in ``image_map`` (``{src_url: {"name", "url"}}``, the
    uploaded-to-Qase inline images), so they render in place. When the image
    isn't in the map (download failed / no session), it falls back to a
    ``[Image: filename]`` text note so the filename is still preserved.
    """
    if not text:
        return ""
    image_map = image_map or {}

    def _img_note(m):
        src = _SRC_RE.search(m.group(0))
        if src:
            url = src.group(1)
            info = image_map.get(url)
            if info and info.get("url"):
                return f" ![{info.get('name') or 'image'}]({info['url']}) "
            # Extract filename from URL (plain filenames and ZS CDN paths).
            fname = url.rstrip("/").split("/")[-1]
            # ZS CDN filenames look like: {uuid}-{timestamp}-{filename}
            cdn_match = _CDN_NAME_RE.match(fname)
            if cdn_match:
                fname = cdn_match.group(1).replace("+", " ")
            return f" [Image: {fname}] "
        return ""

    text = _IMG_TAG_RE.sub(_img_note, text)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return text.strip()


def extract_img_urls(html: str) -> list:
    """Return all ``src`` URLs from ``<img>`` tags in HTML."""
    if not html:
        return []
    return _IMG_SRC_RE.findall(html)
