# Project Context: OCR Photos to Spreadsheet

Last updated: 2026-06-04

## Repository

- Local repo: `C:\Users\User\Documents\Codex\2026-05-27\photo_to_sheet_conversion`
- GitHub remote: `https://github.com/mikegrossbarth/photo_to_sheet_conversion.git`
- Main branch: `main`
- Current known clean pushed head when this file was created: `78041b4 Move export purchase price to third column`

## Goal

Build a Windows desktop app that lets a user upload photos of graded trading card slabs, OCR/extract card inventory details, review/edit rows, and export a clean spreadsheet.

The app was originally inspired by OCR behavior from `live_comps`, but this project intentionally deviates from that approach because it must support multi-card photos and random/non-grid slab layouts.

## Main App Files

- `app/ocr_app.py`
  - Tkinter desktop GUI.
  - Handles adding photos/folders, scanning, row sorting, editable cells, delete selected rows, manual rows, and export.
- `app/multi_card_extraction.py`
  - Gemini OCR/detection pipeline.
  - Detects slab regions, crops each slab, OCRs fields, verifies certs, normalizes grades/attributes/sport.
- `app/xlsx_export.py`
  - CSV/XLSX export schema and formatting.
  - Builds combined card description column.
- `app/sport_lookup.py`
  - Uses bundled player database to infer sport when OCR/model output cannot determine it.
- `app/player_sport_data.json`
  - Bundled player-to-sport lookup database.
- `app/live_comps_ocr/cert_extraction.py`
  - Reused parsing/retry helpers from the live_comps-style OCR tooling.
- `install_dependencies.bat`
  - Installs bundled/local Python environment dependencies.
  - Uses dependency stamp files to avoid reinstalling when requirements have not changed.
- `OCR Photos to Spreadsheet.bat` / `OCR Photos to Spreadsheet.vbs`
  - Windows launchers.

## Environment/API Key

- The app expects `GOOGLE_API_KEY`.
- It loads environment values from:
  - `app\.env`
  - repo-root `.env`
  - an older live_comps env path if present, without overriding existing values.
- Users need a Gemini/Google API key from the paid or free Google AI/GCP project they want billed.
- A `429 Too Many Requests` error means the API key/project hit a quota or rate limit. Increasing billing/quota helps, but the app still makes multiple Gemini calls per multi-card image, so large batches can hit per-minute limits.

## Current User-Facing Features

- Add individual images or folders.
- Scan photos containing one or many graded slabs.
- Designed for random/non-grid layouts; it should not rely on fixed grid slicing.
- Supports PSA, BGS/Beckett, SGC, CGC, TAG, and unknown grading companies.
- Extracted fields include:
  - cert number
  - player/subject
  - year
  - set
  - card number
  - subset
  - parallel
  - attributes
  - grading company
  - grade
  - sport/category
  - confidence
  - raw label text
  - source photo
  - quality score
  - purchase price
- GUI rows auto-sort after scans so high-confidence/high-quality rows are near the top.
- Columns auto-size.
- Double-click editable cells before export.
  - `Cert #` is editable, so users can add certs the OCR missed.
  - Purchase price is editable.
  - Read-only GUI columns are status, quality, file, and card index.
- `Add Manual Card` button adds a blank manual row for cards the app did not detect at all.
  - Manual rows are not sent through OCR on scan.
  - Manual rows export with source photo as `Manual Entry`.
- `Delete Selected` removes individual card rows before export.
- Exports CSV or XLSX.

## Export Format

As of commit `78041b4`, the export columns begin:

1. `Certification Number`
2. `Card Description`
3. `Purchase Price`

Full export order:

1. Certification Number
2. Card Description
3. Purchase Price
4. Card Number
5. Player / Subject
6. Year
7. Set
8. Subset
9. Parallel
10. Grading Company
11. Grade
12. Sport
13. Confidence
14. Is Graded Slab
15. Raw Label Text
16. Position
17. Source Photo
18. Quality Score

The combined `Card Description` should look like:

- `2011 TOPPS UPDATE MIKE TROUT PSA 6`
- `2024 PRIZM REED SHEPPARD WHITE LAZER PSA 10`
- `2020 PANINI FLAWLESS AARON JUDGE RUBY SIGNATURES AUTOGRAPH BGS 9`

Description building intentionally excludes grading condition words/subgrade noise such as:

- `GEM-MT`
- `MINT`
- `NM-MT`
- `NM-MT+`
- `NM`
- `EX-MT`
- `EX`
- `VG-EX`
- `VG`
- `GOOD`
- `FR`
- `PR`
- `HALF-POINT`
- BGS subgrade snippets like `CENTERING 9`, `CORNERS 9`, `EDGES 9`, `SURFACE 9.5`

Grades should be numeric only, never condition text.

## OCR/Detection Notes

Important behavior added during this thread:

- Multi-card photos are processed by detecting slab regions and OCRing each crop.
- The app asks Gemini for slab boxes and label/header boxes, then merges them.
- A label sweep prompt helps catch labels near image edges and simple rows.
- A row-oriented fallback prompt helps with side-by-side slabs but is not a fixed grid assumption.
- Body-only slab crops are expanded upward so the label/header is included.
  - This fixed the Max Clark case where the detector saw the card body but cropped below the PSA label.
- An uncovered-left-edge fallback can add a low-confidence candidate crop when the model misses an obvious left slab.
  - This is only a candidate region; OCR/dedupe must still produce useful data.
- Dedupe/merge prefers taller/full slab regions over small label-only regions when detections overlap.
- Cert verification is required before a row can score perfectly.
- Rotated cert verification exists for sideways or oddly oriented crops.

Known test examples from the thread:

- `709154506_850085721020924_7798130356930279390_n.jpg`
  - Verified Max Clark read after fix:
    - `MAX CLARK`
    - `2025 BOWMAN`
    - cert `115605209`
    - PSA 10
    - quality 100
- Same photo also produced:
  - `LEO DE VRIES`, cert `98825493`, quality 100
  - `SKENES/GRANDAL`, cert `115206636`, quality 100
- `713103102_1637886513983422_5221036276501309607_n.jpg`
  - Acuna and BGS Vladimir Guerrero Jr improved.
  - Jasson Dominguez still had a weak/partial read in one test, so this remains a likely future tuning target.

## Recent Commit Trail

Recent relevant commits when this file was written:

- `78041b4 Move export purchase price to third column`
- `124d058 Move purchase price after cert`
- `cf86dad Add manual card rows before export`
- `a3c2e73 Add purchase price export column`
- `9119e52 Recover missed row slabs`
- `52790c6 Add slab label sweep detection`
- `b24d482 Verify rotated cert mismatches`
- `5cd9887 Read rotated cert fallbacks`
- `960df57 Tune cert verification crop reads`
- `82b332a Verify certs before perfect scores`
- `e112d03 Tighten slab crop scoring`
- `d918a04 Avoid grid-based slab inference`

## Running/Testing

Typical local checks:

```powershell
& 'C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m py_compile app\ocr_app.py app\multi_card_extraction.py app\xlsx_export.py
```

Export order check:

```powershell
& 'C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -c "import sys; sys.path.insert(0, 'app'); from xlsx_export import EXPORT_HEADERS, EXPORT_KEYS; print(EXPORT_HEADERS[:5]); print(EXPORT_KEYS[:5]); print(len(EXPORT_HEADERS), len(EXPORT_KEYS))"
```

Expected export start:

```text
['Certification Number', 'Card Description', 'Purchase Price', 'Card Number', 'Player / Subject']
['cert_number', 'card_description', 'purchase_price', 'card_number', 'player']
18 18
```

## Future Improvement Ideas

- Add app-side throttling/backoff controls for paid/free Gemini API limits so large batches do not cause 429 errors as easily.
- Add a visible "Scan pacing" setting if users are processing many photos.
- Continue improving missed right-side/partial slab recognition, especially cases like Jasson Dominguez where the full slab is visible but OCR confidence is weak.
- Consider extracting visible price stickers/sticky-note values into purchase price, but be careful:
  - user-provided purchase price should remain editable and not overwritten unexpectedly.
  - handwritten sale prices may not always equal purchase price.
- Consider a row-level "Needs Review" filter for low-confidence or missing-cert rows.

## User Preferences Captured

- Accuracy matters more than speed, but API quota/rate limits are a real concern.
- Do not rely on grid assumptions; photos may have random layouts.
- Sort strongest/high-confidence reads to the top after scan.
- Normalize names and major output text to all caps.
- Grades should be numeric only.
- Condition words should not appear as grade or clutter card descriptions.
- Export needs to be clean and spreadsheet-ready.
- Users must be able to edit rows before export, especially certs and purchase price.
