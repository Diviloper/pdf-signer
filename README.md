# Signador de PDFs

Segella PDFs en lot amb una imatge de signatura (o una insígnia amb nom +
marca de temps generada automàticament) i signa'ls digitalment amb un
certificat del magatzem de certificats de Windows — tot des d'una interfície
d'escriptori senzilla.

Requereix **Windows** (llegeix els certificats i les claus privades del
magatzem de certificats de Windows via CNG/NCrypt).

## Opció 1: Executa directament el .exe

Sense Python, sense configuració.

1. Ves a la [pàgina de Releases](https://github.com/Diviloper/pdf-signer/releases)
   i descarrega `Diviloper-PDF-Signer.exe` de l'última versió.
2. Fes-hi doble clic per obrir-lo — Windows pot mostrar un avís de SmartScreen
   ("Windows ha protegit el vostre ordinador") perquè l'exe no està signat
   amb codi; fes clic a **Més informació → Executa igualment**.
3. A l'aplicació:
   1. **1. Fitxers PDFs** — fes clic a **Afegeix fitxers PDF...** i tria
      un o més PDFs per signar.
   2. **2. Certificat** — tria el teu certificat de signatura del
      desplegable (obtingut del magatzem de certificats de Windows). Si té
      un nom/NIF, aquest nom s'omple automàticament a sota.
   3. **3. Segell** — deixa que generi automàticament un segell a partir
      del nom del signant, o tria el teu propi fitxer d'imatge. Fes clic a la
      previsualització de la pàgina per triar on va el segell.
   4. **4. Sortida** — tria la carpeta de sortida (per defecte, la
      carpeta del primer PDF que has afegit).
   5. **5. Executar** — fes clic a **Aplica segell i signa**. Si la clau
      privada del certificat necessita un PIN, Windows el demanarà.
4. En acabar, un missatge emergent et permet signar més documents, obrir els
   fitxers signats, obrir la carpeta de sortida o tancar l'aplicació.

## Opció 2: Executa des del codi font amb uv

Requereix [uv](https://docs.astral.sh/uv/) i Windows.

```
git clone https://github.com/Diviloper/pdf-signer.git
cd pdf-signer
uv sync
uv run main.py
```

`uv sync` crea un `.venv` i instal·la tot des de `pyproject.toml` /
`uv.lock`; `uv run main.py` obre l'aplicació.

## Compilar el .exe tu mateix

```
uv sync
uv run python scripts/build_icon.py
uv run pyinstaller pdf_signer.spec --noconfirm
```

El resultat és `dist/Diviloper-PDF-Signer.exe`. Això també s'executa
automàticament a cada push a `main` via `.github/workflows/release.yml`, que
publica l'exe compilat com una nova versió de GitHub.

---

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
   1. **1. PDF Files** — click **Add PDF Files...** and pick
      one or more PDFs to sign.
   2. **2. Certificate** — pick your signing certificate from
      the dropdown (pulled from your Windows certificate store). If it has
      a name/NIF, that name is auto-filled below.
   3. **3. Stamp** — either let it auto-generate a stamp from the
      signer name, or pick your own image file. Click on the page preview
      to choose where the stamp goes.
   4. **4. Output** — pick the output folder (defaults to the
      folder of the first PDF you added).
   5. **5. Run** — click **Apply Stamp & Sign**. If the
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
