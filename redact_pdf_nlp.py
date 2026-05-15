"""
PDF Redaction Tool — NLP-Variante (spaCy + Presidio)
=====================================================
Erkennt PII via Named Entity Recognition statt Regex.
Regex bleibt für strukturierte Daten (IBAN, Telefon, Steuer-ID).

Installation:
    pip install pymupdf presidio-analyzer presidio-anonymizer
    pip install spacy
    python -m spacy download de_core_news_lg

Verwendung:
    python redact_pdf_nlp.py input.pdf
    python redact_pdf_nlp.py input.pdf --output geschwärzt.pdf
    python redact_pdf_nlp.py input.pdf --dry-run --verbose
"""

import re
import sys
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import pymupdf

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider


# ---------------------------------------------------------------------------
# Presidio Setup mit deutschem spaCy-Modell
# ---------------------------------------------------------------------------

def build_analyzer() -> AnalyzerEngine:
    """Baut den Presidio AnalyzerEngine mit deutschem spaCy-Modell."""
    config = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "de", "model_name": "de_core_news_lg"}],
    }
    provider = NlpEngineProvider(nlp_configuration=config)
    nlp_engine = provider.create_engine()

    analyzer = AnalyzerEngine(
        nlp_engine=nlp_engine,
        supported_languages=["de"],
    )
    return analyzer


# Presidio-Entitäten die geschwärzt werden sollen
PRESIDIO_ENTITIES = [
    "PERSON",           # Namen
    "LOCATION",         # Adressen, Orte
    "ORGANIZATION",     # Firmennamen
    "IBAN_CODE",        # IBAN
    "PHONE_NUMBER",     # Telefon
    "EMAIL_ADDRESS",    # E-Mail
]

# Regex-Fallback für Dinge die NER schlecht erkennt
REGEX_PATTERNS: dict[str, re.Pattern] = {
    # Deutsche Steuer-ID (11 Ziffern)
    "Steuer_ID": re.compile(r"\b[1-9]\d{2}[\s\/]?\d{3}[\s\/]?\d{3}[\s\/]?\d{2}\b"),

    # Steuernummer XX/XXX/XXXXX
    "Steuernummer": re.compile(
        r"\bSteuer(?:nummer|nr\.?)[\s:]*\d{2,3}[\s\/]\d{3,4}[\s\/]\d{4,5}\b",
        re.IGNORECASE,
    ),

    # Kontonummer mit Label
    "Kontonummer": re.compile(
        r"\bKonto(?:nummer|nr\.?)[\s:]*\d[\d\s\-]{7,14}\d\b",
        re.IGNORECASE,
    ),
}

# Konfidenz-Schwellwert für Presidio (0.0 - 1.0)
# Höher = weniger False Positives, aber ggf. mehr False Negatives
MIN_SCORE = 0.6


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------

@dataclass
class RedactionMatch:
    page_num: int
    entity_type: str
    text: str
    rect: pymupdf.Rect


@dataclass
class RedactionResult:
    input_path: str
    output_path: str
    matches: list[RedactionMatch] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.matches)

    def summary(self) -> str:
        if not self.matches:
            return "Keine PII-Treffer gefunden."
        by_type: dict[str, int] = {}
        for m in self.matches:
            by_type[m.entity_type] = by_type.get(m.entity_type, 0) + 1
        lines = [f"Gesamt: {self.total} Treffer"]
        for k, v in sorted(by_type.items()):
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Redaction Engine
# ---------------------------------------------------------------------------

def redact_pdf(
    input_path: str | Path,
    output_path: Optional[str | Path] = None,
    dry_run: bool = False,
    use_ocr: bool = False,
    ocr_language: str = "deu+eng",
    min_score: float = MIN_SCORE,
) -> RedactionResult:
    input_path = Path(input_path)
    if output_path is None:
        output_path = input_path.with_stem(input_path.stem + "_redacted")
    output_path = Path(output_path)

    result = RedactionResult(str(input_path), str(output_path))

    print("Lade NLP-Modell...", flush=True)
    analyzer = build_analyzer()

    doc = pymupdf.open(str(input_path))

    for page_num, page in enumerate(doc):
        if use_ocr:
            text_page = page.get_textpage_ocr(language=ocr_language, dpi=300, full=True)
        else:
            text_page = page.get_textpage()

        # Text zeilenweise extrahieren
        blocks = text_page.extractDICT()["blocks"]
        page_text = ""
        for block in blocks:
            if block.get("type") == 0:
                for line in block.get("lines", []):
                    line_text = "".join(s.get("text", "") for s in line.get("spans", []))
                    page_text += line_text + "\n"

        # --- Presidio NER ---
        presidio_results = analyzer.analyze(
            text=page_text,
            language="de",
            entities=PRESIDIO_ENTITIES,
            score_threshold=min_score,
        )

        for res in presidio_results:
            matched_text = page_text[res.start:res.end].strip()
            if not matched_text:
                continue
            rects = page.search_for(matched_text, textpage=text_page)
            for rect in rects:
                result.matches.append(RedactionMatch(
                    page_num=page_num + 1,
                    entity_type=res.entity_type,
                    text=matched_text,
                    rect=rect,
                ))
                if not dry_run:
                    page.add_redact_annot(rect, fill=(0, 0, 0), text="")

        # --- Regex-Fallback ---
        for pattern_name, pattern in REGEX_PATTERNS.items():
            for match in pattern.finditer(page_text):
                matched_text = match.group(0).strip()
                if not matched_text:
                    continue
                rects = page.search_for(matched_text, textpage=text_page)
                for rect in rects:
                    result.matches.append(RedactionMatch(
                        page_num=page_num + 1,
                        entity_type=pattern_name,
                        text=matched_text,
                        rect=rect,
                    ))
                    if not dry_run:
                        page.add_redact_annot(rect, fill=(0, 0, 0), text="")

        if not dry_run:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)

    if not dry_run:
        doc.set_metadata({})
        doc.save(str(output_path), garbage=4, deflate=True, clean=True)
        print(f"✓ Geschwärztes PDF gespeichert: {output_path}")

    doc.close()
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Schwärzt PII in OCR-PDFs via NLP (spaCy + Presidio)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", help="Pfad zum Quell-PDF")
    parser.add_argument("--output", "-o", help="Pfad für das geschwärzte PDF")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Treffer anzeigen ohne zu schreiben")
    parser.add_argument("--ocr", action="store_true",
                        help="Tesseract-OCR für bildbasierte PDFs")
    parser.add_argument("--ocr-lang", default="deu+eng")
    parser.add_argument("--score", type=float, default=MIN_SCORE,
                        help=f"Minimaler Konfidenz-Score (default: {MIN_SCORE})")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Alle Treffer ausgeben")

    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Fehler: Datei nicht gefunden: {input_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Analysiere: {input_path}")
    if args.dry_run:
        print("→ Dry-Run: keine Änderungen werden gespeichert\n")

    result = redact_pdf(
        input_path=input_path,
        output_path=args.output,
        dry_run=args.dry_run,
        use_ocr=args.ocr,
        ocr_language=args.ocr_lang,
        min_score=args.score,
    )

    print(result.summary())

    if args.verbose and result.matches:
        print("\nDetails:")
        for m in result.matches:
            preview = m.text[:6] + "***" if len(m.text) > 6 else "***"
            print(f"  Seite {m.page_num:>3} | {m.entity_type:<20} | {preview}")


if __name__ == "__main__":
    main()