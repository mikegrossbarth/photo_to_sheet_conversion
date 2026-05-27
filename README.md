# OCR Photos to Spreadsheet

Desktop batch OCR tool for graded card photos. It uses the `live-comps` certification extraction approach, with an added multi-card photo path that can identify every visible graded slab in a group photo, then exports one row per card to `.xlsx` or `.csv`.

## First-time setup

1. Double-click `install_dependencies.bat`. If Python is missing, the installer downloads Python for the current Windows user, creates a local `.venv` folder, and installs the app dependencies.
2. Copy `app\.env.example` to `app\.env`.
3. Put your Gemini key in `app\.env` as `GOOGLE_API_KEY=...`.

If you already have a configured `live-comps\.env`, the app will also read that automatically.

If the installer closes after showing an error, open `install_dependencies.bat` again from File Explorer and read the message above "Press any key to continue." The most common issue is a blocked Python download or no internet connection.

## Open the app

Double-click `OCR Photos to Spreadsheet.vbs` for the clean GUI launcher, or use `OCR Photos to Spreadsheet.bat` if you want a console window for troubleshooting.

## Workflow

1. Click `Add Pictures` or `Add Folder`.
2. Click `Scan`.
3. Review the extracted rows.
4. Click `Export Spreadsheet`.

Single-card photos produce one row. Multi-card photos use a two-pass flow: first the app detects visible slab/card-holder regions, then it crops each region and runs the single-card OCR on each crop. This produces one row per detected slab, with card number and position fields so you can audit the group photo quickly.

For best certification-number accuracy, use the sharpest original photo available. If a group photo is blurry or labels are small, the app will still create card rows, but unreadable cert numbers may be left blank.

Exports are saved wherever you choose, with `outputs\` as the default folder.
