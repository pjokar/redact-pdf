# pdf-blackout

CLI-Tool zum automatischen Schwärzen sensibler Daten in deutschen PDFs mit eingebettetem OCR-Textlayer.

## Setup

```bash
pip install pymupdf spacy
python -m spacy download de_core_news_lg
```

## Verwendung

```bash
python redact_pdf.py input.pdf                 # Output: input_geschwärzt.pdf
python redact_pdf.py input.pdf -o output.pdf   # Eigener Pfad
python redact_pdf.py input.pdf --preview       # Treffer anzeigen, nichts schreiben
```

## Architektur

Einzige Datei: `redact_pdf.py`

**Pipeline pro Seite:**
1. `page.get_text("text")` → Rohtext
2. Regex-Scan über alle PATTERNS → Treffer-Rects
3. spaCy NER (PER-only) auf bereinigtem Text → weitere Treffer-Rects
4. `_merge_rects()` → überlappende Rechtecke zusammenführen
5. `page.add_redact_annot()` + `page.apply_redactions()` → schwarze Blöcke

## Was geschwärzt wird

Nur drei Kategorien:

| Kategorie | Pattern |
|---|---|
| **Namen** | `name_label` (label-basiert), spaCy PER-Entities |
| **Bankdaten** | `iban`, `bic_swift`, `kontonummer`, `blz`, `kreditkarte`, `verwendungszweck` |
| **Adressen** | `strassenadresse`, `plz_ort` |

## Was bewusst NICHT geschwärzt wird

- Datumsangaben (DD.MM.YYYY, YYYY-MM-DD, ausgeschriebene Monatsnamen)
- €-Beträge / EUR-Beträge
- Spalten- und Tabellenüberschriften (Saldo, Brutto, Netto, Haben, Soll …)
- E-Mail-Adressen, Telefonnummern, Ausweis-/Passnummern
- Steuer-ID, Steuernummer, USt-ID, Sozialversicherungsnummer
- Immobiliendaten (Grundbuch, Kataster, Zinssatz, Flächen etc.)

## Regex-Patterns

Alle Pattern werden mit `_add(name, pattern, flags=re.IGNORECASE)` registriert.

**Wichtige Designentscheidungen:**

- **Capturing-Group → nur Wert schwärzen**: Patterns mit Label (z. B. `Kontonummer: (\d+)`) verwenden eine Capturing-Group. `_find_regex_hits` nimmt `match.group(match.lastindex)` — das Label bleibt lesbar, nur der Wert wird geschwärzt.
- **`bic_swift` mit `flags=0`** (kein IGNORECASE): BIC-Codes sind immer Großbuchstaben. Mit IGNORECASE würden 8- oder 11-stellige Alltagswörter wie "Darlehen", "Kontonummer" fälschlich matchen.
- **`kontonummer` erfordert Suffix oder Doppelpunkt**: "Konto" allein trifft Spaltenköpfe neben Beträgen — erst "Kontonummer", "Konto-Nr.", "Kto." oder "Konto:" löst einen Match aus.
- **`kreditkarte_ablauf` nicht vorhanden**: MM/YY matcht zu viele Referenznummern und Periodenangaben (01/2024) in Finanzberichten.
- **`plz_ort` mit End-Anker `\b`** und ohne `\s` im Zeichensatz: verhindert, dass der Rest des Satzes nach der PLZ mitgefresst wird.

## spaCy NER

- Nur `PER`-Entities — `ORG`, `LOC`, `MISC` erzeugen zu viele Fehlalarme auf Finanzterminologie.
- Text-Bereinigung vor NLP: `\n → " "` damit Tabellenzellen wie `"Max\nMüller"` als eine Person erkannt werden.
- Suche immer wortweise (nicht als ganzen String): `page.search_for("Max Müller")` schlägt bei PDF-Layout-Boxen fehl; Token-für-Token-Suche ist robust.
- `_NER_STOPWORDS`: Finanzterme (netto, brutto, saldo, haben …), die `de_core_news_lg` fälschlich als Personennamen klassifiziert.

## Bekannte Grenzen

- PDFs ohne OCR-Textlayer (reine Scan-Bilder) werden übersprungen (Warnung pro Seite).
- spaCy `de_core_news_lg` erkennt Namen in stark fragmentierten Tabellentexten nicht zuverlässig — Workaround: `name_label`-Pattern greifen über Labels (Inhaber:, Empfänger: etc.) zuverlässig in strukturierten Feldern.
- BIC-Pattern trifft nur echte Großschrift-BICs — BICs in Kleinschrift werden nicht erkannt (Akzeptiert: seltener Grenzfall).

## Abhängigkeiten

| Paket | Zweck |
|---|---|
| `pymupdf` (fitz) | PDF lesen, Textstellen suchen, Schwärzungen anwenden |
| `spacy` + `de_core_news_lg` | NER für Personennamen in Fließtext |
