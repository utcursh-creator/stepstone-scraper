"""Tests for CV file-type detection — the fix for unopenable scraped CVs.

Root cause (reported by Umair 2026-06-01): every CV was downloaded and uploaded
as application/pdf under a .pdf name regardless of its real format. A Word or
image CV was therefore stored in Recruitee as a broken "CV.pdf" the viewer could
not open. These tests pin (a) the magic-byte sniffing that picks the correct
extension and (b) the filename->MIME mapping used on upload.
"""
from scraper.profile import _sniff_cv_type
from utils.recruitee import _mime_for_filename


# >=64 bytes each so the minimum-size guard passes; real CVs are KB-MB.
_PAD = b"\x00" * 80


def test_sniffs_pdf():
    assert _sniff_cv_type(b"%PDF-1.7\n" + _PAD) == ("pdf", "application/pdf")


def test_sniffs_docx_word_zip():
    # OOXML .docx is a zip (PK\x03\x04) containing word/document.xml
    buf = b"PK\x03\x04" + _PAD + b"word/document.xml" + _PAD
    ext, mime = _sniff_cv_type(buf)
    assert ext == "docx"
    assert "wordprocessingml" in mime


def test_sniffs_legacy_doc_ole2():
    buf = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + _PAD
    assert _sniff_cv_type(buf) == ("doc", "application/msword")


def test_sniffs_jpeg():
    ext, mime = _sniff_cv_type(b"\xff\xd8\xff\xe0" + _PAD)
    assert ext == "jpg"
    assert mime == "image/jpeg"


def test_sniffs_png():
    ext, mime = _sniff_cv_type(b"\x89PNG\r\n\x1a\n" + _PAD)
    assert ext == "png"
    assert mime == "image/png"


def test_sniffs_odt():
    buf = b"PK\x03\x04mimetypeapplication/vnd.oasis.opendocument.text" + _PAD
    ext, _ = _sniff_cv_type(buf)
    assert ext == "odt"


def test_generic_zip_defaults_to_docx():
    # A zip with no recognised marker is most likely a Word doc.
    buf = b"PK\x03\x04" + _PAD
    assert _sniff_cv_type(buf)[0] == "docx"


def test_html_error_page_rejected():
    # StepStone sometimes returns an HTML interstitial with HTTP 200 — not a CV.
    buf = b"<!DOCTYPE html><html><head><title>Fehler</title></head>" + _PAD
    assert _sniff_cv_type(buf) is None


def test_empty_rejected():
    assert _sniff_cv_type(b"") is None


def test_too_short_rejected():
    assert _sniff_cv_type(b"%PDF") is None


def test_mime_for_filename_pdf():
    assert _mime_for_filename("Max_Mustermann_CV.pdf") == "application/pdf"


def test_mime_for_filename_docx():
    assert "wordprocessingml" in _mime_for_filename("Max_CV.docx")


def test_mime_for_filename_doc():
    assert _mime_for_filename("Max_CV.doc") == "application/msword"


def test_mime_for_filename_jpg_uppercase():
    assert _mime_for_filename("Max_CV.JPG") == "image/jpeg"


def test_mime_for_filename_no_extension():
    assert _mime_for_filename("CV") == "application/octet-stream"
