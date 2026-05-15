"""
PDF Redaction Tool für Immobiliendokumente
==========================================
Schwärzt PII (Namen, IBAN, Adressen, Steuernummern, E-Mail, Telefon)
in OCR-PDFs — entfernt sowohl Bildpixel als auch den OCR-Textlayer.

Abhängigkeiten:
    pip install pymupdf

Optionale Abhängigkeit für scanned PDFs ohne Textlayer:
    sudo apt install tesseract-ocr tesseract-ocr-deu  (Linux)
    brew install tesseract                             (macOS)

Verwendung:
    python redact_pdf.py input.pdf
    python redact_pdf.py input.pdf --output geschwärzt.pdf
    python redact_pdf.py input.pdf --dry-run   # zeigt Treffer ohne zu schwärzen
"""

import re
import sys
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import pymupdf  # pip install pymupdf


# ---------------------------------------------------------------------------
# PII-Pattern (Deutschland-fokussiert, Immobiliendokumente)
# ---------------------------------------------------------------------------

PII_PATTERNS: dict[str, re.Pattern] = {
    # IBAN: DE + 20 Ziffern, optional mit Leerzeichen alle 4 Stellen
    "IBAN": re.compile(
        r"\b(DE\d{2}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{2})\b",
        re.IGNORECASE,
    ),

    # Kontonummer: 8–12-stellig, ggf. mit Trennzeichen
    "Kontonummer": re.compile(r"\bKonto(?:nummer|nr\.?)[\s:]*(\d[\d\s\-]{7,14}\d)\b", re.IGNORECASE),

    # Deutsche Steuer-ID (11 Ziffern, erste nicht 0)
    "Steuer_ID": re.compile(r"\b([1-9]\d{2}[\s\/]?\d{3}[\s\/]?\d{3}[\s\/]?\d{2})\b"),

    # Steuernummer (Format variiert je Bundesland: XX/XXX/XXXXX)
    "Steuernummer": re.compile(
        r"\b(Steuer(?:nummer|nr\.?)[\s:]*\d{2,3}[\s\/]\d{3,4}[\s\/]\d{4,5})\b",
        re.IGNORECASE,
    ),

    # E-Mail
    "Email": re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),

    # Telefon/Fax: diverse deutsche Formate
    "Telefon": re.compile(
        r"(?:Tel\.?|Fax\.?|Mobil\.?|Phone[\s:]*)?(?:\+49|0049|0)[\s\-]?(\d{2,5})[\s\-\/]?(\d{3,}[\s\-]?\d*)",
        re.IGNORECASE,
    ),

    # PLZ + Ort (5-stellige PLZ gefolgt von Wort)
    "PLZ_Ort": re.compile(r"\b(\d{5})\s+([A-ZÄÖÜ][a-zäöüß\-]+(?:\s[A-ZÄÖÜ][a-zäöüß\-]+)*)\b"),

    # Straße + Hausnummer — Bindestrich-Straßennamen (Ernst-Reuter-Str.) + optionaler Ort
    # group(0) wird für search_for verwendet (siehe Matching-Loop)
    "Strasse": re.compile(
        r"[A-ZÄÖÜ][A-Za-zäöüÄÖÜß\-]+(?:[\s][A-Za-zäöüÄÖÜß\-]+)*"
        r"(?:straße|strasse|str\.|gasse|weg|allee|platz|damm|ring|ufer|chaussee)"
        r"\s*\d{1,4}\s*[a-zA-Z]?(?:\s*[-\/]\s*\d{1,4}\s*[a-zA-Z]?)?"
        r"(?:[,]\s*[A-ZÄÖÜ][a-zäöüß]+)?",
        re.IGNORECASE,
    ),

    # Namen mit Anrede: Herr/Frau + Vor- Nachname
    "Name_Anrede": re.compile(
        r"\b(?:Herr|Frau|Hr\.|Fr\.)\s+(?:[A-ZÄÖÜ][a-zäöüß\-]+\s+){1,2}[A-ZÄÖÜ][a-zäöüß\-]+\b"
    ),

    # Namen ohne Anrede: Nachname Vorname (Immobilien-Abrechnungsformat)
    # Erkennt zwei aufeinanderfolgende kapitalisierte deutsche Wörter (mind. 3 Zeichen)
    # nach einem Zeilenumbruch oder bekanntem Label (Eigentümer, Mieter, etc.)
    "Name_Label": re.compile(
        r"(?:Eigentümer|Mieter|Käufer|Verkäufer|Vermieter|Darlehensnehmer"
        r"|Auftraggeber|Wohnungseigentümer)[\s:]*"
        r"([A-ZÄÖÜ][a-zäöüß\-]{2,}\s+[A-ZÄÖÜ][a-zäöüß\-]{2,})\b",
        re.IGNORECASE,
    ),

    # Hauptmieter/Eigentümer: direkt nach "Vorausz." — Personen UND Firmen
    # Hauptmieter/Eigentümer: direkt nach "0,00 Vorausz." — Personen UND Firmen
    # Nur matchen wenn Betrag in derselben Zeile (nicht Zusammenfassungszeilen)
    # Hauptmieter/Eigentümer: nach "0,00 Vorausz." — Personen UND Firmen
    # \s* vor Zahl wegen führenden Leerzeichen im PDF-Textlayer
    "Name_Vorausz": re.compile(
        r"(?:\s*\d[\d\.,]*\s*Vorausz\.\n)"
        r"([A-ZÄÖÜ][^\n]{2,60})"
        r"(?=\n)",
    ),

    # Zweiter Bewohner/Miteigentümer: steht allein direkt vor "Abrechnung"
    # Optionaler Präfix: "f. " (für), "f./ " etc.
    # Ausschluss von Zeilen mit Kleinwörtern wie "für", "und" (Notizen, keine Namen)
    # Zweiter Bewohner/Miteigentümer: direkt vor "Abrechnung" oder "Seite X"
    # Punkt im Zeichensatz für "Steinpreis f. Waldemar"
    "Name_Zweiter": re.compile(
        r"^\s*(?:f[.]\s*[/]?\s*)?"
        r"([A-ZÄÖÜ][A-Za-zäöüÄÖÜß\.\- ]+[A-Za-zäöüÄÖÜß])\s*\n"
        r"(?=Abrechnung|Seite\s)",
        re.MULTILINE,
    ),
}


# ---------------------------------------------------------------------------
# Redaction Engine
# ---------------------------------------------------------------------------

@dataclass
class RedactionMatch:
    page_num: int
    pattern_name: str
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
            by_type[m.pattern_name] = by_type.get(m.pattern_name, 0) + 1
        lines = [f"Gesamt: {self.total} Treffer"]
        for k, v in sorted(by_type.items()):
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)


def redact_pdf(
    input_path: str | Path,
    output_path: Optional[str | Path] = None,
    dry_run: bool = False,
    use_ocr: bool = False,
    ocr_language: str = "deu+eng",
) -> RedactionResult:
    """
    Schwärzt PII in einem PDF.

    Args:
        input_path:   Pfad zum Quell-PDF
        output_path:  Pfad für das geschwärzte PDF (default: input_redacted.pdf)
        dry_run:      Nur analysieren, nicht schreiben
        use_ocr:      Tesseract für rein bildbasierte PDFs aktivieren
        ocr_language: Tesseract-Sprache(n), z.B. "deu+eng"

    Returns:
        RedactionResult mit allen Treffern
    """
    input_path = Path(input_path)
    if output_path is None:
        output_path = input_path.with_stem(input_path.stem + "_redacted")
    output_path = Path(output_path)

    result = RedactionResult(str(input_path), str(output_path))

    doc = pymupdf.open(str(input_path))

    for page_num, page in enumerate(doc):
        # Textlayer ermitteln — bei Bedarf via OCR
        if use_ocr:
            text_page = page.get_textpage_ocr(language=ocr_language, dpi=300, full=True)
        else:
            text_page = page.get_textpage()

        # Text-Blöcke mit Koordinaten — Zeilenumbrüche erhalten für Name_Liste-Pattern
        blocks = text_page.extractDICT()["blocks"]
        page_text = ""
        for block in blocks:
            if block.get("type") == 0:  # Textblock
                for line in block.get("lines", []):
                    line_text = ""
                    for span in line.get("spans", []):
                        line_text += span.get("text", "")
                    page_text += line_text + "\n"

        # Pattern-Matching
        KLEINWORT_RE = re.compile(r'\b(für|und|oder|der|die|das|am|im|vom|bei|mit)\b(?!\s+[A-ZÄÖÜ])', re.IGNORECASE)
        AUSSCHLUSS_RE = re.compile(
            r'^(Abrechnung|Vorausz|Jahresabrechnung|Einzelabrechnung|Abrechnungs\w*'
            r'|Objekt|Seite|Gesamt\b|Saldo|Brutto|Netto|Umsatz|Betrag|Summe'
            r'|Tage|Datum|Zeitraum|Ja|Nein|LEV|GdWE|WEG'
            r'|\*)',
            re.IGNORECASE,
        )
        for pattern_name, pattern in PII_PATTERNS.items():
            for match in pattern.finditer(page_text):
                # Bei Patterns mit Capture Group nur die Gruppe suchen
                matched_text = match.group(1) if match.lastindex else match.group(0)
                matched_text = matched_text.strip()
                if not matched_text:
                    continue
                # Name_Zweiter: bekannte Nicht-Namen und Kleinwörter ausschließen
                if pattern_name == "Name_Zweiter" and (
                    KLEINWORT_RE.search(matched_text) or AUSSCHLUSS_RE.match(matched_text)
                ):
                    continue
                # Name_Vorausz: Zusammenfassungszeilen ausschließen
                if pattern_name == "Name_Vorausz" and AUSSCHLUSS_RE.match(matched_text):
                    continue
                # Bounding Boxes aller Vorkommen auf der Seite suchen
                rects = page.search_for(matched_text, textpage=text_page)
                for rect in rects:
                    result.matches.append(
                        RedactionMatch(
                            page_num=page_num + 1,
                            pattern_name=pattern_name,
                            text=matched_text,
                            rect=rect,
                        )
                    )
                    if not dry_run:
                        # Schwarze Schwärzung — entfernt Bild UND Textlayer
                        page.add_redact_annot(
                            rect,
                            fill=(0, 0, 0),    # schwarze Füllung
                            text="",           # kein Ersatztext
                        )

        if not dry_run:
            # apply_redactions entfernt Pixel + zugehörige Text-Streams
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_PIXELS,  # Bildpixel schwärzen
            )

    if not dry_run:
        # Metadaten bereinigen (Autor, Titel etc. können PII enthalten)
        doc.set_metadata({})
        doc.save(
            str(output_path),
            garbage=4,       # alle ungenutzten Objekte entfernen
            deflate=True,    # komprimieren
            clean=True,      # PDF-Struktur bereinigen
        )
        print(f"✓ Geschwärztes PDF gespeichert: {output_path}")

    doc.close()
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Schwärzt PII in OCR-PDFs (Immobiliendokumente)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", help="Pfad zum Quell-PDF")
    parser.add_argument("--output", "-o", help="Pfad für das geschwärzte PDF")
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Treffer anzeigen ohne zu schreiben",
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="Tesseract-OCR aktivieren (für rein bildbasierte PDFs)",
    )
    parser.add_argument(
        "--ocr-lang",
        default="deu+eng",
        help="Tesseract-Sprache(n), default: deu+eng",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Alle Treffer mit Text und Seite ausgeben",
    )

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
    )

    print(result.summary())

    if args.verbose and result.matches:
        print("\nDetails:")
        for m in result.matches:
            # Text kürzen für Ausgabe — nicht den vollen Match loggen (Datenschutz)
            preview = m.text[:6] + "***" if len(m.text) > 6 else "***"
            print(f"  Seite {m.page_num:>3} | {m.pattern_name:<20} | {preview}")


if __name__ == "__main__":
    main()