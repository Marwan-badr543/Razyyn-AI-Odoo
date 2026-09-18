# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE
#
# GENERATED FILE -- DO NOT EDIT. Copied from the Frappe app's ocr.py by
# tools/sync_from_frappe.py, which replaces its two framework imports with the
# shim below and changes nothing else.
#
# WHY IT IS COPIED RATHER THAN REWRITTEN
#     Reading a figure off a photograph correctly is the whole value of this
#     file, and every number in it -- the page layout mode, the two-pages-at-once
#     batching, the confidence threshold below which a picture is declared to
#     have no words in it -- was measured rather than chosen. A second
#     implementation would be a second set of guesses.

# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""Reading the words out of a photographed or scanned document.

WHY THIS LIVES IN THE ERP AND NOT IN THE AGENT SERVICE
	An accountant photographs a stack of supplier invoices and asks for entries
	to be created from them. Sending twenty photographs to a language model is
	slow, expensive, and unreliable at exactly the thing that matters most —
	reading a figure correctly. Tesseract is a purpose-built reader, it runs on
	the ERP server the practice already pays for, and it turns a photograph into
	the plain text the agent is genuinely good at reasoning about. What travels
	to the service is text, so the pictures themselves never leave the site.

WHAT HAPPENS TO A PICTURE WITH NO WORDS IN IT
	Then it is not a financial document — a logo, a photograph of a shop, a
	screenshot of a chart — and the text would be worse than useless. The
	original picture is sent instead and the model looks at it directly. Nothing
	is ever dropped: every upload reaches the agent as one thing or the other.

WHO DOES THE WORK, AND WHEN
	The background worker that is already running the customer's message, at the
	start of that turn, before the request goes out. One worker owns the whole
	job from the moment "send" is pressed until the answer comes back, and it
	reports its progress to the chat as it goes.

	It is never an ERP web worker. Those serve every other page of the ERP, and
	a colleague opening a report must not wait behind somebody's ten-page scan.

	The answer is kept beside the file, so the same attachment sent again in a
	later message is not read a second time.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from typing import Optional

import logging as _logging

_logger = _logging.getLogger(__name__)


class _FrappeShim:
    """The two calls ocr.py makes into its framework, answered by Odoo's log."""

    @staticmethod
    def log_error(title="", message=""):
        _logger.warning("Razyyn AI OCR: %s: %s", title, message)


frappe = _FrappeShim()

#: Tesseract language codes. Set by ocr_service.py from the site's own
#: configuration before any reading starts -- the page workers that do the
#: reading must not reach into the database themselves.
OCR_LANGUAGES = "eng+ara"


def get_ocr_languages() -> str:
    return OCR_LANGUAGES


#: Written beside the upload when words were found: what gets sent instead.
TEXT_SUFFIX: str = ".ocr.txt"

#: Written beside the upload when the original file is what should be sent —
#: a picture with no words in it, or a PDF that already carries its own text.
#: A marker rather than an absent file, because "not read yet" and "nothing to
#: extract" are different answers and must not look the same.
SKIP_SUFFIX: str = ".ocr.skip"

#: Pictures worth reading. A PDF is included because a scanned invoice is
#: usually a PDF wrapped around a photograph.
_IMAGE_EXTENSIONS: frozenset = frozenset({
	".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
	".heic", ".heif", ".avif",
})

#: 200 dots per inch is the accepted floor for reading printed text reliably.
#: Measured against a practice's own scans: below this the faint grey figures
#: in a totals column — the ones an accountant actually needs — start coming
#: back as letters, while the rest of the page still reads perfectly, so
#: nothing about the page's own confidence gives the mistake away.
_PDF_RENDER_DPI: int = 200

#: A rendered page above this many pixels is scaled back to it. A page whose
#: paper size is declared in a huge unit renders enormous at any DPI, and past
#: this size Tesseract's time climbs while its reading does not improve.
_MAX_PAGE_PIXELS: int = 30_000_000

#: A photograph smaller than this is enlarged before reading. Tesseract needs
#: something like ten pixels of letter height, and a receipt photographed as a
#: thumbnail has three.
_SMALL_PICTURE_PIXELS: int = 2000

#: Below this, a page's own text layer is treated as absent and the page is
#: read as a picture. A scanned page often carries a stray character or two from
#: a header stamp, so "any text at all" is the wrong test.
_MIN_EMBEDDED_CHARS_PER_PAGE: int = 60

#: A word shorter than this is punctuation or speckle, and averaging confidence
#: over speckle says nothing about whether there is a document here.
_MIN_WORD_LENGTH: int = 2

#: What a real document looks like to Tesseract. Photographs of walls, logos and
#: gradients produce a handful of stray marks that it reports with low
#: confidence; printed text produces many tokens it is sure about.
#:
#: THE DECISION IS CONFIDENCE, NOT LENGTH. Counting characters cannot tell a
#: small receipt from noise: both are short. Tesseract already computes how sure
#: it is of every word, and that is the number that separates them.
_MIN_CONFIDENT_WORDS: int = 4
_MIN_MEAN_CONFIDENCE: float = 55.0

#: How the page is assumed to be laid out, best first.
#:
#: "4" IS A COLUMN OF LINES OF VARYING SIZE, and that is what an invoice is: a
#: heading, a party block, a table of figures, a totals box. Read that way the
#: table keeps its rows — "1 Sneakers SKU005 150 100.00 15,000.00" — which is
#: the only form in which a quantity and its price mean anything.
#:
#: The layout this replaced assumed a whole page with its own orientation, and
#: it scored better on confidence while quietly dropping every figure in the
#: table: an invoice came back as its customer and its heading, with no
#: amounts at all, and the desk was left saying it could not read the document.
#: Confidence measures how sure Tesseract is of the words it found. It says
#: nothing about the words it never looked for.
#:
#: "6" is one solid block, which is what a close-up of a till receipt is. It is
#: tried only when the first reading is not confident, so it costs nothing on
#: the documents that read cleanly.
_LAYOUTS: tuple = ("4", "6")

#: Reading one picture. Long enough for a dense A4 scan, short enough that a
#: pathological input cannot occupy a worker indefinitely.
_TESSERACT_TIMEOUT_SECONDS: int = 90

#: Pages of one PDF that are read as pictures. A document longer than this is
#: not refused and nothing is dropped silently — the extracted text says plainly
#: which pages were not read, so the customer and the agent both know.
_MAX_OCR_PAGES: int = 50

#: Told the filename, which page of it is being read, and how many there are.
Progress = Callable[[str, int, int], None] | None


# ─── What is worth reading ───────────────────────────────────────────────────

def _extension(path: str) -> str:
	return os.path.splitext(path.lower())[1]


def is_readable_document(path: str) -> bool:
	"""True for the uploads whose text has to be recovered before it can be used."""
	return _extension(path) in _IMAGE_EXTENSIONS or _extension(path) == ".pdf"


def text_path(stored_path: str) -> str:
	return stored_path + TEXT_SUFFIX


def skip_path(stored_path: str) -> str:
	return stored_path + SKIP_SUFFIX


# ─── Reading a picture ───────────────────────────────────────────────────────

def _prepare(image):
	"""Give Tesseract the best version of this picture that costs nothing.

	Grey rather than colour, because colour carries no information about letter
	shapes and triples the work. Very small pictures are enlarged; very large
	ones are brought down, because past a point the extra pixels buy time and
	nothing else.
	"""
	from PIL import Image

	prepared = image.convert("L")

	longest = max(prepared.size)
	if longest < _SMALL_PICTURE_PIXELS:
		scale = _SMALL_PICTURE_PIXELS / max(1, longest)
	elif prepared.width * prepared.height > _MAX_PAGE_PIXELS:
		scale = (_MAX_PAGE_PIXELS / (prepared.width * prepared.height)) ** 0.5
	else:
		return prepared

	return prepared.resize(
		(max(1, int(prepared.width * scale)), max(1, int(prepared.height * scale))),
		Image.LANCZOS,
	)


def _lines_of(data: dict) -> str:
	"""The words Tesseract found, put back into the lines it found them on.

	Tesseract reports every word with the block, paragraph and line it belongs
	to, so the page can be reassembled from the same answer that carries the
	confidences. It used to be asked twice — once for the confidences and again
	for the text — which is two full readings of every page for one page of
	output, and it was the whole reason a ten-page scan took two minutes.
	"""
	lines: list = []
	current: list = []
	at = None

	for index, word in enumerate(data.get("text", [])):
		where = (
			data["block_num"][index], data["par_num"][index], data["line_num"][index],
		)
		if where != at:
			if current:
				lines.append(" ".join(current))
			current, at = [], where
		word = (word or "").strip()
		if word:
			current.append(word)

	if current:
		lines.append(" ".join(current))
	return "\n".join(line for line in lines if line.strip())


def _read_with_layout(image, page_layout: str, languages: str) -> tuple:
	"""Read one picture under one page-layout assumption.

	Returns (text, number of confident words, mean confidence).
	"""
	import pytesseract
	from pytesseract import Output

	data = pytesseract.image_to_data(
		image,
		lang=languages,
		config=f"--psm {page_layout}",
		output_type=Output.DICT,
		timeout=_TESSERACT_TIMEOUT_SECONDS,
	)

	confidences = []
	for word, confidence in zip(data.get("text", []), data.get("conf", []), strict=False):
		cleaned = (word or "").strip()
		try:
			score = float(confidence)
		except (TypeError, ValueError):
			continue
		if len(cleaned) < _MIN_WORD_LENGTH or score < 0:
			continue
		confidences.append(score)

	if not confidences:
		return "", 0, 0.0

	return _lines_of(data), len(confidences), sum(confidences) / len(confidences)


def _is_a_document(words: int, confidence: float) -> bool:
	return words >= _MIN_CONFIDENT_WORDS and confidence >= _MIN_MEAN_CONFIDENCE


def read_image_text(image, languages: str = "") -> str:
	"""The words in one picture, or "" if there is no document in it.

	Raises if the reader itself could not run — that is not the same answer as
	"there are no words here", and a caller that cannot tell them apart records
	a photograph of a supplier invoice as a picture of nothing.

	The languages are passed in rather than looked up, because the pages of a
	long document are read on worker threads and the site's own settings belong
	to the thread that opened the site.
	"""
	languages = languages or get_ocr_languages()
	prepared = _prepare(image)

	best_text, best_words, best_confidence = "", 0, 0.0
	problems: list = []
	for page_layout in _LAYOUTS:
		try:
			text, words, confidence = _read_with_layout(prepared, page_layout, languages)
		except Exception as exc:
			problems.append(f"layout {page_layout}: {exc}")
			continue

		if words * confidence > best_words * best_confidence:
			best_text, best_words, best_confidence = text, words, confidence

		if _is_a_document(best_words, best_confidence):
			break

	if not best_words and problems:
		raise OSError("; ".join(problems))

	return best_text if _is_a_document(best_words, best_confidence) else ""


def _read_image_file(path: str, languages: str = "") -> str:
	from PIL import Image

	# A picture large enough to exhaust memory is a denial of service, not a
	# receipt. Pillow's own guard, made explicit rather than left at whatever
	# another app on this site happened to set it to.
	Image.MAX_IMAGE_PIXELS = 120_000_000

	with Image.open(path) as image:
		image.load()
		return read_image_text(image, languages)


# ─── Reading a PDF ───────────────────────────────────────────────────────────

def _page_count(path: str) -> int:
	"""How many pages this PDF has, asked of the PDF rather than inferred.

	The page count CANNOT be taken from the text extractor's output. A scanned
	document has no text at all, so every one of its pages comes back empty and
	counting non-empty pieces says the document has none — which is exactly the
	document this whole feature exists for.
	"""
	try:
		completed = subprocess.run(
			["pdfinfo", path], capture_output=True, timeout=30,
		)
	except Exception:
		return 0

	if completed.returncode != 0:
		return 0

	for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
		if line.startswith("Pages:"):
			try:
				return int(line.split(":", 1)[1].strip())
			except ValueError:
				return 0
	return 0


def _embedded_pages(path: str) -> list:
	"""The text each page already carries, one entry per page of the document.

	Read with poppler's own extractor, which is already required for rendering
	pages and so adds nothing to install. A form feed separates pages; the list
	is then squared up against the real page count, so a page with nothing on it
	is present as an empty string rather than missing.
	"""
	total = _page_count(path)
	if total <= 0:
		return []

	try:
		completed = subprocess.run(
			["pdftotext", "-layout", "-enc", "UTF-8", path, "-"],
			capture_output=True,
			timeout=_TESSERACT_TIMEOUT_SECONDS,
		)
		extracted = (
			completed.stdout.decode("utf-8", errors="replace").split("\f")
			if completed.returncode == 0 else []
		)
	except Exception:
		extracted = []

	pages = (extracted + [""] * total)[:total]
	return pages


def _pages_needing_reading(pages: list) -> list:
	"""Which page numbers carry no text of their own.

	THE DECISION IS PER PAGE, NOT PER DOCUMENT. Accounting PDFs are routinely
	mixed: a generated invoice with a scanned receipt stapled on the end, or a
	statement whose covering page is a photocopy. Judging the whole document by
	its first page either sends forty typed pages through a reader they do not
	need, or skips the one page that was a photograph.
	"""
	return [
		number for number, text in enumerate(pages, start=1)
		if len((text or "").strip()) < _MIN_EMBEDDED_CHARS_PER_PAGE
	]


def _render_page(path: str, page_number: int):
	from pdf2image import convert_from_path

	# PPM, not PNG. Nothing keeps these images: they are read once and dropped.
	# Compressing each one on the way out cost four fifths of the time spent
	# turning a ten-page scan into pages — more than the reading itself.
	pages = convert_from_path(
		path,
		dpi=_PDF_RENDER_DPI,
		first_page=page_number,
		last_page=page_number,
		thread_count=1,
	)
	return pages[0] if pages else None


def _read_one_page(path: str, number: int, languages: str) -> tuple:
	"""One page of a PDF, as (page number, its words, what went wrong).

	RUNS ON A PAGE WORKER AND SO TOUCHES NOTHING OF FRAPPE'S. The site
	connection, the cache and the error log all belong to the background worker
	that started this turn; reaching for any of them from another thread is how
	a reading fails for a reason that has nothing to do with the document.
	Anything that goes wrong comes back as the third value and is recorded by
	the thread that is allowed to record it.
	"""
	image = None
	try:
		image = _render_page(path, number)
		return number, (read_image_text(image, languages) if image is not None else ""), ""
	except Exception as exc:
		return number, "", str(exc)
	finally:
		if image is not None:
			image.close()


#: How many pages of one document are read at once.
#:
#: TWO, MEASURED. One page at a time reads four pages of a practice's scan in
#: 21 seconds; two at a time do it in 11; three in 12 and four in 23, because
#: Tesseract already spreads one page across cores and past that the workers
#: are competing for the same ones. Two is also the number that leaves the
#: machine to everybody else: a colleague running a report while somebody reads
#: a ten-page scan must not feel it.
_PAGES_AT_ONCE: int = 2


def read_pdf_text(path: str, on_page=None, languages: str = "") -> str:
	"""The text of a PDF, reading only the pages that need reading.

	Returns "" when the PDF already carries all of its own text — then the file
	itself is what should be sent, and it keeps its layout by going as a PDF.
	"""
	from concurrent.futures import ThreadPoolExecutor

	languages = languages or get_ocr_languages()

	pages = _embedded_pages(path)
	if not pages:
		return ""

	needs_reading = _pages_needing_reading(pages)
	if not needs_reading:
		return ""

	unread = [number for number in needs_reading if number > _MAX_OCR_PAGES]
	needs_reading = [number for number in needs_reading if number <= _MAX_OCR_PAGES]

	if on_page:
		on_page(0, len(needs_reading))

	read: dict = {}
	done = 0
	with ThreadPoolExecutor(max_workers=_PAGES_AT_ONCE) as pages_at_once:
		for number, text, problem in pages_at_once.map(
			lambda page: _read_one_page(path, page, languages), needs_reading,
		):
			read[number] = text
			if problem:
				frappe.log_error(
					title="Accountant Agent: OCR page failed",
					message=f"Page {number} of {os.path.basename(path)}: {problem}",
				)
			done += 1
			if on_page:
				on_page(done, len(needs_reading))

	assembled = []
	found_any = False
	for number, existing in enumerate(pages, start=1):
		if number in unread:
			continue
		page_text = read[number] if number in read else (existing or "").strip()
		if page_text:
			found_any = True
			# EVERY PAGE UNDER ITS OWN HEADING. A stack of invoices scanned into
			# one file is a stack of separate documents, and a wall of text with
			# no divisions in it invites the reader to run two of them together.
			assembled.append(f"--- Page {number} of {len(pages)} ---\n{page_text}")

	if unread:
		# Named, never silent: a customer who sends a hundred-page scan must be
		# told which part of it was read, not left to assume all of it was.
		assembled.append(
			f"--- Note ---\nPages {unread[0]} to {unread[-1]} are pictures and were "
			f"not read; only the first {_MAX_OCR_PAGES} pages of a scanned document "
			"are read automatically."
		)
		found_any = True

	return "\n\n".join(assembled) if found_any else ""


# ─── One upload, from end to end ─────────────────────────────────────────────

def pages_to_read(stored_path: str) -> int:
	"""How much reading this upload is, before any of it is done.

	Zero means there is nothing to do — the file is not a picture, or it is a
	PDF that already carries its own text. That is what lets the chat say
	nothing at all about a typed PDF instead of announcing work it never did.
	"""
	if not is_readable_document(stored_path):
		return 0
	if os.path.exists(text_path(stored_path)) or os.path.exists(skip_path(stored_path)):
		return 0
	if _extension(stored_path) != ".pdf":
		return 1

	pages = _embedded_pages(stored_path)
	if not pages:
		return 0
	return len([n for n in _pages_needing_reading(pages) if n <= _MAX_OCR_PAGES])


def reading_of(stored_path: str) -> str | None:
	"""The words already read out of this upload, or None. Never reads anything.

	None means "send the file itself": there is nothing to extract, or nobody
	has asked for it to be read.
	"""
	if not os.path.exists(text_path(stored_path)):
		return None
	try:
		with open(text_path(stored_path), encoding="utf-8") as handle:
			return handle.read() or None
	except Exception:
		return None


def read_upload(stored_path: str, on_progress: Progress = None) -> str | None:
	"""Read one upload now, and remember the answer beside it.

	Returns the text to send instead of the file, or None to send the file
	itself. Reading the same attachment again in a later message costs nothing:
	the answer from the first time is still there.

	A failure is an answer too. Whatever happens, one of the two markers is
	left, so nothing ever asks for this file to be read a second time in the
	hope of a different result — the picture is simply sent as it is.
	"""
	if not is_readable_document(stored_path):
		return None

	already = reading_of(stored_path)
	if already is not None:
		return already
	if os.path.exists(skip_path(stored_path)):
		return None

	name = os.path.basename(stored_path)
	# Read once, here, where the site is open: the pages of a long document are
	# read on worker threads that must not reach for the site's own settings.
	languages = get_ocr_languages()
	try:
		if _extension(stored_path) == ".pdf":
			text = read_pdf_text(
				stored_path,
				on_page=(lambda done, total: on_progress(name, done, total))
				if on_progress else None,
				languages=languages,
			)
		else:
			if on_progress:
				on_progress(name, 0, 1)
			text = _read_image_file(stored_path, languages)
			if on_progress:
				on_progress(name, 1, 1)
	except Exception as exc:
		frappe.log_error(
			title="Accountant Agent: OCR failed",
			message=f"Could not read {name}: {exc}",
		)
		text = ""

	try:
		if text:
			with open(text_path(stored_path), "w", encoding="utf-8") as handle:
				handle.write(text)
		else:
			with open(skip_path(stored_path), "w", encoding="utf-8") as handle:
				handle.write("")
	except Exception as exc:
		frappe.log_error(
			title="Accountant Agent: OCR result not saved",
			message=f"Could not record the reading of {name}: {exc}",
		)

	return text or None
