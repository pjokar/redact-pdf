# pip install pymupdf spacy
# python -m spacy download de_core_news_lg

import argparse
import re
import sys
from pathlib import Path

import fitz  # pymupdf

# --- spaCy mit Fallback ---
try:
    import spacy
    nlp = spacy.load("de_core_news_lg")
    SPACY_AVAILABLE = True
except (ImportError, OSError) as e:
    print(f"[WARNUNG] spaCy nicht verfügbar ({e}). Fallback auf Regex-only.", file=sys.stderr)
    SPACY_AVAILABLE = False

# ---------------------------------------------------------------------------
# Regex-Muster
# ---------------------------------------------------------------------------

PATTERNS: list[tuple[str, re.Pattern]] = []

def _add(name: str, pattern: str, flags: int = re.IGNORECASE) -> None:
    PATTERNS.append((name, re.compile(pattern, flags)))

# Patterns mit einer Capturing-Group verwenden group(1) als Suchwort,
# sodass nur der Wert (nicht das vorangestellte Label) geschwärzt wird.

# --- Persönliche Daten ---

_add("email",
     r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_add("telefon",
     r"(?:\+49|0049|0)\s*[\(\-]?\d{2,5}[\)\-\s]?\s*\d{3,}[\s\-]?\d{0,6}"
     r"(?:[\s\-]\d{1,4})?")

_add("ausweis_pass",
     r"\b[A-Z]{1,2}\d{6,9}[A-Z0-9]?\b")

# --- Bankdaten / Finanzdaten ---

_add("iban",
     r"\b[A-Z]{2}\d{2}(?:\s*\d{4}){4,6}\s*\d{0,3}\b")

_add("bic_swift",
     r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b")

_add("kontonummer",
     r"(?:Konto(?:nummer|\.?-?Nr\.?)?|Kto\.?):?\s*\d[\d\s]{4,14}")

_add("blz",
     r"(?:BLZ|Bankleitzahl):?\s*\d{8}")

_add("kreditkarte",
     r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2})"
     r"[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{1,4}\b")

_add("kreditkarte_ablauf",
     r"\b(?:0[1-9]|1[0-2])/(?:\d{2}|\d{4})\b")

# Nur der Wert nach dem Doppelpunkt wird geschwärzt, nicht das Label selbst.
_add("empfaenger_auftraggeber",
     r"(?:Empfänger|Auftraggeber)\s*:\s*(.+?)(?=\n|$)")

_add("verwendungszweck",
     r"Verwendungszweck\s*:\s*(.+?)(?=\n|$)")

_add("steuer_id",
     r"(?:Steuer(?:identifikationsnummer|[-\s]?ID|[-\s]?Nr\.?)|IdNr\.?)"
     r"\s*:?\s*\d[\d\s/]{8,12}")

_add("steuernummer",
     r"(?:Steuernummer|St\.?[-\s]?Nr\.?)\s*:?\s*\d{2,3}/\d{3,4}/\d{4,5}")

_add("ust_id",
     r"\bDE\d{9}\b")

_add("sozialversicherung",
     r"\b\d{2}[\s]?\d{6}[A-Z]\d{3}\b")

# --- Immobiliendaten ---

_add("grundbuch",
     r"(?:Grundbuch(?:blatt|nummer|amt)?|Blatt|Flur(?:stück)?)\s*"
     r":?\s*(?:[A-Za-zÄÖÜäöüß\s]+\s+)?(?:Nr\.?\s*)?\d+(?:[/\-]\d+)?")

_add("kataster",
     r"(?:Gemarkung|Flurnummer|Flurstück(?:snummer)?)\s*:?\s*"
     r"[A-Za-zÄÖÜäöüß\s]*\s*\d+(?:[/\-]\d+)?")

_add("grundstuecksgroesse",
     r"\d{1,6}(?:[.,]\d{1,2})?\s*(?:m²|qm|Quadratmeter)")

_add("darlehenskonto",
     r"(?:Darlehenskonto(?:nummer)?|Darlehens[-\s]?Nr\.?)\s*:?\s*\d[\d\s]{4,20}")

_add("wohnflaeche",
     r"(?:Wohnfläche|Nutzfläche|Gesamtfläche)\s*:?\s*"
     r"\d{1,5}(?:[.,]\d{1,2})?\s*(?:m²|qm)")

_add("belegenheit",
     r"(?:Belegenheit|Belegenheitsadresse|Objekt(?:adresse)?)\s*:?\s*"
     r"[A-Za-zÄÖÜäöüßéàü\s\-\.]+\s+\d+[a-z]?"
     r"(?:\s*,\s*\d{5}\s+[A-Za-zÄÖÜäöüß\s]+)?")

# --- Adressen ---

_add("strassenadresse",
     r"\b[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]+(?:straße|str\.|gasse|weg|allee|platz|"
     r"ring|damm|chaussee|pfad|steig|graben|ufer|kai)\b"
     r"[\s,]+\d+[a-z]?(?:[-–]\d+[a-z]?)?"
     r"(?:\s*[,/]\s*(?:Wohnung|Wg\.?|App\.?|Etage|OG|EG|DG)\s*\d+[a-z]?)?")

_add("plz_ort",
     r"\b\d{5}\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\s\-]{2,}")

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

MIN_MATCH_LEN = 3


def _merge_rects(rects: list[fitz.Rect]) -> list[fitz.Rect]:
    """Überlappende oder berührende Rechtecke zusammenführen."""
    if not rects:
        return []
    sorted_rects = sorted(rects, key=lambda r: (round(r.y0, 1), r.x0))
    merged = [sorted_rects[0]]
    for rect in sorted_rects[1:]:
        last = merged[-1]
        # Gleiche Zeile (y-Überlapp) und x-Überlapp / Berührung
        if rect.y0 <= last.y1 + 2 and rect.x0 <= last.x1 + 2:
            merged[-1] = fitz.Rect(
                min(last.x0, rect.x0),
                min(last.y0, rect.y0),
                max(last.x1, rect.x1),
                max(last.y1, rect.y1),
            )
        else:
            merged.append(rect)
    return merged


def _find_regex_hits(page: fitz.Page) -> list[tuple[str, fitz.Rect]]:
    """Alle Regex-Treffer auf einer Seite lokalisieren."""
    hits: list[tuple[str, fitz.Rect]] = []
    text = page.get_text("text")
    if not text.strip():
        return hits

    for label, pattern in PATTERNS:
        for match in pattern.finditer(text):
            # Patterns mit Capturing-Group: nur den Wert (group 1) suchen,
            # nicht das vorangestellte Label mitschärzen.
            matched = (match.group(1) if match.lastindex else match.group()).strip()
            if len(matched) < MIN_MATCH_LEN:
                continue
            quads = page.search_for(matched, quads=True)
            for quad in quads:
                hits.append((label, quad.rect))
    return hits


def _find_spacy_hits(page: fitz.Page) -> list[tuple[str, fitz.Rect]]:
    """Alle spaCy-NER-Treffer auf einer Seite lokalisieren."""
    hits: list[tuple[str, fitz.Rect]] = []
    if not SPACY_AVAILABLE:
        return hits

    text = page.get_text("text")
    if not text.strip():
        return hits

    doc = nlp(text)
    relevant_labels = {"PER", "ORG", "LOC", "GPE", "MISC"}
    for ent in doc.ents:
        if ent.label_ not in relevant_labels:
            continue
        token = ent.text.strip()
        if len(token) < MIN_MATCH_LEN:
            continue
        quads = page.search_for(token, quads=True)
        for quad in quads:
            hits.append((f"NER:{ent.label_}", quad.rect))
    return hits


def _collect_hits(page: fitz.Page) -> list[tuple[str, fitz.Rect]]:
    regex_hits = _find_regex_hits(page)
    spacy_hits = _find_spacy_hits(page)
    return regex_hits + spacy_hits


# ---------------------------------------------------------------------------
# Haupt-Verarbeitung
# ---------------------------------------------------------------------------

def redact_pdf(input_path: Path, output_path: Path, preview: bool = False) -> None:
    if not input_path.exists():
        print(f"[FEHLER] Datei nicht gefunden: {input_path}", file=sys.stderr)
        sys.exit(1)

    try:
        doc = fitz.open(str(input_path))
    except Exception as e:
        print(f"[FEHLER] Datei kann nicht geöffnet werden (kein gültiges PDF?): {e}", file=sys.stderr)
        sys.exit(1)

    total_hits = 0

    for page_num, page in enumerate(doc, start=1):
        text = page.get_text("text")
        if not text.strip():
            print(f"[WARNUNG] Seite {page_num}: kein extrahierbarer Text — übersprungen.")
            continue

        hits = _collect_hits(page)

        if not hits:
            print(f"  Seite {page_num}: 0 Treffer")
            continue

        rects_by_label: dict[str, list[fitz.Rect]] = {}
        for label, rect in hits:
            rects_by_label.setdefault(label, []).append(rect)

        all_rects: list[fitz.Rect] = []
        for label, rects in rects_by_label.items():
            merged = _merge_rects(rects)
            all_rects.extend(merged)

        all_rects = _merge_rects(all_rects)

        page_count = len(all_rects)
        total_hits += page_count
        print(f"  Seite {page_num}: {page_count} Treffer")

        if preview:
            for label, rect in hits:
                # Originaltext für die Vorschau aus dem Treffer rekonstruieren
                snippet = page.get_textbox(rect).replace("\n", " ").strip()
                print(f"    [{label}] {snippet!r}")
            continue

        for rect in all_rects:
            page.add_redact_annot(rect, fill=(0, 0, 0))

        page.apply_redactions()

    print(f"\nGesamt: {total_hits} Treffer")

    if preview:
        print("[VORSCHAU] Keine Datei geschrieben.")
        doc.close()
        return

    try:
        doc.save(str(output_path), garbage=4, deflate=True)
        print(f"Geschwärzte PDF gespeichert: {output_path}")
    except Exception as e:
        print(f"[FEHLER] Datei konnte nicht gespeichert werden: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Schwärzt sensible Daten in PDFs mit eingebettetem OCR-Text."
    )
    parser.add_argument("input", type=Path, help="Pfad zur Eingabe-PDF")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Pfad zur Ausgabe-PDF (Standard: <input>_geschwärzt.pdf)"
    )
    parser.add_argument(
        "--preview", action="store_true",
        help="Treffer anzeigen ohne zu schreiben"
    )
    args = parser.parse_args()

    input_path: Path = args.input
    if args.output:
        output_path: Path = args.output
    else:
        output_path = input_path.with_name(
            input_path.stem + "_geschwärzt" + input_path.suffix
        )

    print(f"Verarbeite: {input_path}")
    if not args.preview:
        print(f"Ausgabe:    {output_path}")
    print()

    redact_pdf(input_path, output_path, preview=args.preview)


if __name__ == "__main__":
    main()
