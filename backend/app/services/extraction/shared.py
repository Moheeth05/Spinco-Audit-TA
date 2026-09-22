"""Shared, single instances of the OCR engine and text extractor - every
extraction entry point (full document extraction, bill-only re-extraction)
reuses these rather than each constructing its own."""

from app.services.extraction.ocr_engine import TesseractOCREngine
from app.services.extraction.text_extraction import PyMuPDFTextExtractor

ocr_engine = TesseractOCREngine()
text_extractor = PyMuPDFTextExtractor(ocr_engine=ocr_engine)
