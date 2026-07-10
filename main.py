"""Entry point for the PDF Batch Stamper & Signer."""

import sys

import gui


def main() -> None:
    if sys.platform != "win32":
        print("This tool requires Windows (it signs using the Windows certificate store).")
        sys.exit(1)
    gui.run()


if __name__ == "__main__":
    main()
