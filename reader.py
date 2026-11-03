"""Рендер страниц PDF в JPEG, чтобы книги читались в любом браузере, включая телефон.

PyMuPDF не потокобезопасен, а waitress обслуживает запросы в нескольких потоках,
поэтому вся работа с документами идёт под одной блокировкой. Открытые документы
кешируются: крупные PDF открываются заметное время.
"""
import threading
from collections import OrderedDict
from pathlib import Path

import pymupdf

MIN_WIDTH, MAX_WIDTH = 400, 2000
JPEG_QUALITY = 80
MAX_OPEN_DOCS = 4
BLANK_COVER_BRIGHTNESS = 236  # средняя яркость 0–255: выше — страница почти пустая


class PdfRenderer:
    def __init__(self):
        self._lock = threading.Lock()
        self._docs: OrderedDict[Path, pymupdf.Document] = OrderedDict()

    def _open(self, path: Path) -> pymupdf.Document:
        doc = self._docs.pop(path, None)
        if doc is None:
            doc = pymupdf.open(path)
            if len(self._docs) >= MAX_OPEN_DOCS:
                _, oldest = self._docs.popitem(last=False)
                oldest.close()
        self._docs[path] = doc  # в конец — как самый свежий
        return doc

    def page_count(self, path: Path) -> int:
        with self._lock:
            return self._open(path).page_count

    def render_cover(self, path: Path, width: int) -> bytes | None:
        """Первая страница как обложка; None, если это почти белый титульный лист."""
        with self._lock:
            first = self._open(path)[0]
            probe = first.get_pixmap(dpi=20, colorspace=pymupdf.csGRAY)
            brightness = sum(probe.samples) / max(1, len(probe.samples))
        if brightness > BLANK_COVER_BRIGHTNESS:
            return None
        return self.render(path, 1, width)

    def render(self, path: Path, page: int, width: int) -> bytes:
        """page — номер страницы с 1; width — желаемая ширина картинки в пикселях."""
        width = max(MIN_WIDTH, min(MAX_WIDTH, width))
        with self._lock:
            doc = self._open(path)
            if not 1 <= page <= doc.page_count:
                raise IndexError(page)
            pdf_page = doc[page - 1]
            zoom = width / pdf_page.rect.width
            pix = pdf_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            return pix.tobytes("jpg", jpg_quality=JPEG_QUALITY)
