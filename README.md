# PDF Signer

Batch-stamp PDFs with a signature image (or an auto-generated name + timestamp
badge) and digitally sign them with a certificate from the Windows
certificate store — all from a simple desktop GUI.

Requires **Windows** (it reads certificates and private keys from the
Windows certificate store via CNG/NCrypt).

## Option 1: Just run the .exe

No Python, no setup.

1. Go to the [Releases page](https://github.com/Diviloper/pdf-signer/releases)
   and download `Diviloper-PDF-Signer.exe` from the latest release.
2. Double-click it to launch — Windows may show a SmartScreen warning
   ("Windows protected your PC") since the exe isn't code-signed; click
   **More info → Run anyway**.
3. In the app:
   1. **1. Fitxers PDF / PDF Files** — click **Add PDF Files...** and pick
      one or more PDFs to sign.
   2. **2. Certificat / Certificate** — pick your signing certificate from
      the dropdown (pulled from your Windows certificate store). If it has
      a name/NIF, that name is auto-filled below.
   3. **3. Segell / Stamp** — either let it auto-generate a stamp from the
      signer name, or pick your own image file. Click on the page preview
      to choose where the stamp goes.
   4. **4. Sortida / Output** — pick the output folder (defaults to the
      folder of the first PDF you added).
   5. **5. Executar / Run** — click **Apply Stamp & Sign**. If the
      certificate's private key needs a PIN, Windows will prompt for it.
4. When done, a popup lets you sign more documents, open the signed files,
   open the output folder, or close the app.

## Option 2: Run from source with uv

Requires [uv](https://docs.astral.sh/uv/) and Windows.

```
git clone https://github.com/Diviloper/pdf-signer.git
cd pdf-signer
uv sync
uv run main.py
```

`uv sync` creates a `.venv` and installs everything from `pyproject.toml` /
`uv.lock`; `uv run main.py` launches the app.

## Building the .exe yourself

```
uv sync
uv run python scripts/build_icon.py
uv run pyinstaller pdf_signer.spec --noconfirm
```

The result is `dist/Diviloper-PDF-Signer.exe`. This also runs automatically
on every push to `main` via `.github/workflows/release.yml`, which publishes
the built exe as a new GitHub release.
