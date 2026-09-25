"""Recognise a CV file from its bytes, whatever the server called it.

Moved out of scraper/profile.py (DirectSearch, retired in September 2026) so
the Stepstone Recruit unlock can use it too.
"""


def sniff_cv_type(buffer: bytes) -> tuple[str, str] | None:
    """Detect (extension, mime_type) from a file's magic bytes.

    Returns None when the bytes are not a usable document — empty, too small, or
    an HTML error page (StepStone occasionally answers a download with an HTTP-200
    interstitial). Sniffing exists because candidates upload CVs as PDF *or* Word
    *or* image; storing a Word/image CV under a .pdf name + application/pdf MIME is
    exactly why Recruitee could not open some scraped CVs.
    """
    if not buffer or len(buffer) < 64:
        return None
    head = buffer[:16]
    if head[:4] == b"%PDF":
        return ("pdf", "application/pdf")
    if head[:3] == b"\xff\xd8\xff":
        return ("jpg", "image/jpeg")
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ("png", "image/png")
    if head[:5] == b"{\\rtf":
        return ("rtf", "application/rtf")
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":  # OLE2 → legacy MS Office .doc
        return ("doc", "application/msword")
    if head[:4] == b"PK\x03\x04":  # zip container → OOXML (.docx) or ODF (.odt)
        if b"opendocument.text" in buffer[:1024]:
            return ("odt", "application/vnd.oasis.opendocument.text")
        if b"word/" in buffer:
            return ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        if b"xl/" in buffer:
            return ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if b"ppt/" in buffer:
            return ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        # Unknown zip — for a CV the overwhelmingly likely case is a Word doc.
        return ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    # Anything else (HTML interstitial, login page, garbage) is not a CV.
    return None
