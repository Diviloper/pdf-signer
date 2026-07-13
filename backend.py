"""Core processing backend for PDF Batch Stamper & Signer.

Handles GUI/PDF coordinate translation, digital signing using a
certificate pulled from the Windows certificate store (via CNG/NCrypt
through ctypes) combined with pyHanko, and rendering the stamp image as
the visible appearance of the new signature field pyHanko creates for
that signature.
"""

from __future__ import annotations

import ctypes
import hashlib
import re
import sys
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Callable, List, Optional

import fitz  # PyMuPDF
from cryptography import x509 as cryptography_x509
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import NameOID
from PIL import Image as PILImage
from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.generic import pdf_name
from pyhanko.pdf_utils.images import PdfImage
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.layout import (
    AxisAlignment,
    BoxConstraints,
    InnerScaling,
    Margins,
    SimpleBoxLayoutRule,
)
from pyhanko.sign.fields import prepare_sig_field
from pyhanko.sign.general import SigningError
from pyhanko.sign.signers import PdfSignatureMetadata, PdfSigner, Signer
import pyhanko.sign.signers.cms_embedder as _cms_embedder
from pyhanko.stamp import StaticStampStyle


class PdfSignerError(Exception):
    """Raised for any error in the stamp/sign pipeline."""


# ---------------------------------------------------------------------------
# Coordinate translation
# ---------------------------------------------------------------------------


@dataclass
class StampPlacement:
    """A stamp's position and size, expressed in PDF point space and
    anchored to the top-left corner of the page it was chosen on."""

    x: float
    y: float
    width: float
    height: float

    def rect(self) -> fitz.Rect:
        return fitz.Rect(self.x, self.y, self.x + self.width, self.y + self.height)


def gui_point_to_pdf_point(
    gui_x: float,
    gui_y: float,
    canvas_width: float,
    canvas_height: float,
    pdf_width: float,
    pdf_height: float,
) -> tuple[float, float]:
    """Translate a pixel click on the preview canvas into PDF point space.

    PyMuPDF and PyQt6 both use a top-left (0, 0) origin, so only scaling
    (no vertical flip) is required.
    """
    if canvas_width <= 0 or canvas_height <= 0:
        raise PdfSignerError("Canvas has zero size; cannot translate coordinates.")
    scale_x = pdf_width / canvas_width
    scale_y = pdf_height / canvas_height
    return gui_x * scale_x, gui_y * scale_y


def pdf_point_to_gui_point(
    pdf_x: float,
    pdf_y: float,
    canvas_width: float,
    canvas_height: float,
    pdf_width: float,
    pdf_height: float,
) -> tuple[float, float]:
    """Inverse of :func:`gui_point_to_pdf_point`: used to re-project a
    remembered PDF-space point onto a canvas that was just re-rendered at
    a different size (e.g. after a window resize)."""
    if pdf_width <= 0 or pdf_height <= 0:
        raise PdfSignerError("PDF page has zero size; cannot translate coordinates.")
    return pdf_x * canvas_width / pdf_width, pdf_y * canvas_height / pdf_height


def stamp_placement_for_click(
    gui_x: float,
    gui_y: float,
    canvas_width: float,
    canvas_height: float,
    pdf_width: float,
    pdf_height: float,
    stamp_width_pt: float,
    stamp_image_px_size: tuple[int, int],
) -> StampPlacement:
    """Build a StampPlacement anchored at the clicked point (top-left corner),
    clamped so the stamp never falls outside the reference page."""
    pdf_x, pdf_y = gui_point_to_pdf_point(
        gui_x, gui_y, canvas_width, canvas_height, pdf_width, pdf_height
    )
    img_w_px, img_h_px = stamp_image_px_size
    if img_w_px <= 0:
        raise PdfSignerError("Stamp image has invalid dimensions.")
    aspect = img_h_px / img_w_px
    stamp_height_pt = stamp_width_pt * aspect

    pdf_x = min(max(pdf_x, 0.0), max(pdf_width - stamp_width_pt, 0.0))
    pdf_y = min(max(pdf_y, 0.0), max(pdf_height - stamp_height_pt, 0.0))

    return StampPlacement(
        x=pdf_x, y=pdf_y, width=stamp_width_pt, height=stamp_height_pt
    )


# ---------------------------------------------------------------------------
# Stamping
# ---------------------------------------------------------------------------

_PDF_HEADER_MARKER = b"%PDF-"
_PDF_HEADER_SEARCH_WINDOW = 1024  # per spec, header must appear in the first 1024 bytes


def _strip_leading_garbage(data: bytes) -> bytes:
    """Drop any bytes before the ``%PDF-`` marker.

    Some download proxies prepend raw HTTP response headers to the saved
    file. Byte offsets in the PDF's cross-reference table are counted
    from the start of the file, so that extra prefix shifts every offset
    and corrupts the file for strict parsers (pyHanko) even though
    lenient ones (PyMuPDF) silently tolerate it.
    """
    idx = data.find(_PDF_HEADER_MARKER, 0, _PDF_HEADER_SEARCH_WINDOW)
    if idx < 0:
        raise PdfSignerError("Not a valid PDF file (missing %PDF header).")
    return data[idx:] if idx > 0 else data


def _clamp_rect_to_page(rect: fitz.Rect, page_rect: fitz.Rect) -> fitz.Rect:
    """Keep the stamp anchored to the top-left corner but shrink/shift it
    so it still lands fully on pages with different (e.g. mixed
    orientation) dimensions."""
    width = min(rect.width, page_rect.width)
    height = min(rect.height, page_rect.height)
    x0 = min(rect.x0, page_rect.width - width)
    y0 = min(rect.y0, page_rect.height - height)
    return fitz.Rect(x0, y0, x0 + width, y0 + height)


# ---------------------------------------------------------------------------
# Auto-generated text stamp (name + timestamp)
# ---------------------------------------------------------------------------

# Fixed template size in PDF points: kept constant regardless of text content
# so a stamp placement chosen on the preview (from this same template) still
# lines up with the image actually generated at signing time.
TEXT_STAMP_WIDTH_PT = 260.0
TEXT_STAMP_HEIGHT_PT = 76.0
TEXT_STAMP_ZOOM = 4.0
# Fraction of the card's shorter side (draw_rect's radius is relative, not in
# points) -- picked to read like a ~20px CSS border-radius at this card size.
TEXT_STAMP_CORNER_RADIUS = 0.26

_STAMP_BORDER_COLOR = (0.298, 0.733, 0.090)  # kelly green
_STAMP_FILL_COLOR = (1, 1, 1)  # white card, so text stays legible on any page
_STAMP_LABEL_COLOR = (0.392, 0.455, 0.545)  # muted gray
_STAMP_NAME_COLOR = (0.118, 0.161, 0.231)  # dark slate
_STAMP_TIME_COLOR = (0.392, 0.455, 0.545)  # muted gray

DEFAULT_SIGNED_BY_LABEL = "Signed digitally by:"
DEFAULT_TIMESTAMP_LABEL = "Timestamp:"


def _format_timestamp_with_tz(timestamp: datetime) -> str:
    """Format ``timestamp`` with its UTC offset. Naive datetimes (e.g. from
    ``datetime.now()``) are assumed to be in the local timezone."""
    aware = timestamp if timestamp.tzinfo is not None else timestamp.astimezone()
    offset = aware.strftime("%z") or "+0000"
    return f"{aware.strftime('%Y-%m-%d %H:%M:%S')} UTC{offset[:3]}:{offset[3:]}"


def _insert_shrink_to_fit(
    page: fitz.Page,
    rect: fitz.Rect,
    text: str,
    fontsizes: tuple,
    fontname: str,
    color: tuple,
    align: int,
) -> None:
    for fontsize in fontsizes:
        overflow = page.insert_textbox(
            rect, text, fontsize=fontsize, fontname=fontname, color=color, align=align
        )
        if overflow >= 0:
            return


def generate_text_stamp_image(
    name: str,
    timestamp: datetime,
    signed_by_label: str = DEFAULT_SIGNED_BY_LABEL,
    timestamp_label: str = DEFAULT_TIMESTAMP_LABEL,
    width_pt: float = TEXT_STAMP_WIDTH_PT,
    height_pt: float = TEXT_STAMP_HEIGHT_PT,
    zoom: float = TEXT_STAMP_ZOOM,
) -> bytes:
    """Render a rounded-corner "signed by" badge showing a small label, the
    signer's name and the signing time (with UTC offset), as a PNG with a
    transparent background outside the rounded border (so it isn't a stark
    white rectangle on the page). The image's pixel size is always
    ``(width_pt, height_pt) * zoom``, independent of the text, so it can
    stand in for a user-picked image file anywhere in the pipeline."""
    if not name.strip():
        raise PdfSignerError("Signer name is required to generate a stamp image.")

    doc = fitz.open()
    try:
        page = doc.new_page(width=width_pt, height=height_pt)

        pad = 4
        card_rect = fitz.Rect(pad, pad, width_pt - pad, height_pt - pad)
        page.draw_rect(
            card_rect,
            color=_STAMP_BORDER_COLOR,
            fill=_STAMP_FILL_COLOR,
            width=2,
            radius=TEXT_STAMP_CORNER_RADIUS,
        )

        inner_pad = 10
        label_rect = fitz.Rect(
            card_rect.x0 + inner_pad,
            card_rect.y0 + 3,
            card_rect.x1 - inner_pad,
            card_rect.y0 + card_rect.height * 0.26,
        )
        name_rect = fitz.Rect(
            card_rect.x0 + inner_pad,
            card_rect.y0 + card_rect.height * 0.26,
            card_rect.x1 - inner_pad,
            card_rect.y0 + card_rect.height * 0.74,
        )
        time_row_rect = fitz.Rect(
            card_rect.x0 + inner_pad,
            card_rect.y0 + card_rect.height * 0.74,
            card_rect.x1 - inner_pad,
            card_rect.y1 - 3,
        )

        _insert_shrink_to_fit(
            page,
            label_rect,
            signed_by_label,
            (8, 7, 6),
            "helv",
            _STAMP_LABEL_COLOR,
            fitz.TEXT_ALIGN_LEFT,
        )
        _insert_shrink_to_fit(
            page,
            name_rect,
            name.strip(),
            (15, 13, 11, 9, 7),
            "hebo",
            _STAMP_NAME_COLOR,
            fitz.TEXT_ALIGN_CENTER,
        )
        # "Timestamp:" and the datetime value share the same row: the label
        # pinned to the left edge, the value centered across the full row.
        _insert_shrink_to_fit(
            page,
            time_row_rect,
            timestamp_label,
            (9, 8, 7, 6),
            "helv",
            _STAMP_TIME_COLOR,
            fitz.TEXT_ALIGN_LEFT,
        )
        _insert_shrink_to_fit(
            page,
            time_row_rect,
            _format_timestamp_with_tz(timestamp),
            (9, 8, 7, 6),
            "helv",
            _STAMP_TIME_COLOR,
            fitz.TEXT_ALIGN_CENTER,
        )

        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True)
        return pix.tobytes("png")
    finally:
        doc.close()


def write_text_stamp_image(
    path: Path,
    name: str,
    timestamp: datetime,
    signed_by_label: str = DEFAULT_SIGNED_BY_LABEL,
    timestamp_label: str = DEFAULT_TIMESTAMP_LABEL,
) -> None:
    """Generate a text stamp image (see :func:`generate_text_stamp_image`)
    and write it to ``path``."""
    path.write_bytes(
        generate_text_stamp_image(name, timestamp, signed_by_label, timestamp_label)
    )


# ---------------------------------------------------------------------------
# Windows certificate store access (CNG / NCrypt via ctypes)
# ---------------------------------------------------------------------------


@dataclass
class CertificateInfo:
    """A signing-capable certificate found in the current user's Windows
    'MY' (Personal) certificate store."""

    thumbprint: bytes
    subject: str
    issuer: str
    not_after: str
    owner_name: Optional[str] = None
    owner_nif: Optional[str] = None

    @property
    def thumbprint_hex(self) -> str:
        return self.thumbprint.hex().upper()

    def __str__(self) -> str:
        return f"{self.subject}  (expires {self.not_after})"


_NIF_PREFIXES = ("IDCES-", "IDESP-", "VATES-", "IDCAT-", "NIF:", "NIF-")
_NIF_PATTERN = re.compile(r"^[0-9XYZ][0-9]{7}[A-Z]$", re.IGNORECASE)
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _clean_nif(raw: str) -> str:
    value = raw.strip().upper()
    for prefix in _NIF_PREFIXES:
        if value.startswith(prefix):
            return value[len(prefix) :].strip()
    return value


def extract_certificate_owner(
    cert: cryptography_x509.Certificate,
) -> tuple[Optional[str], Optional[str]]:
    """Best-effort extraction of the owner's name and NIF/NIE/DNI from a
    certificate's Subject, following the ETSI EN 319 412-1 conventions used
    by Spanish qualified certificates (FNMT, DNIe, Camerfirma, ...)."""
    subject = cert.subject

    def first(oid) -> Optional[str]:
        values = subject.get_attributes_for_oid(oid)
        return values[0].value.strip() if values else None

    given_name = first(NameOID.GIVEN_NAME)
    surname = first(NameOID.SURNAME)
    common_name = first(NameOID.COMMON_NAME)
    serial_number = first(NameOID.SERIAL_NUMBER)

    name: Optional[str] = None
    if given_name and surname:
        name = f"{given_name} {surname}"
    elif common_name and not _UUID_PATTERN.match(common_name):
        # Some issuers append " - DNI 12345678A" to the common name; strip it
        # so it doesn't duplicate the separately-extracted NIF.
        name = re.split(
            r"\s*-\s*(?:DNI|NIF|NIE)\b", common_name, maxsplit=1, flags=re.IGNORECASE
        )[0].strip()

    nif: Optional[str] = None
    if serial_number:
        candidate = _clean_nif(serial_number)
        if _NIF_PATTERN.match(candidate):
            nif = candidate
    if nif is None and common_name:
        match = re.search(
            r"\b(?:DNI|NIF|NIE)[:\s]*([0-9XYZ][0-9]{7}[A-Z])\b",
            common_name,
            re.IGNORECASE,
        )
        if match:
            nif = match.group(1).upper()

    return name, nif


if sys.platform == "win32":
    crypt32 = ctypes.WinDLL("crypt32.dll")
    ncrypt = ctypes.WinDLL("ncrypt.dll")

    CERT_STORE_PROV_SYSTEM_W = 10
    CERT_SYSTEM_STORE_CURRENT_USER = 1 << 16
    CERT_KEY_PROV_INFO_PROP_ID = 2
    CERT_FIND_HASH = 1 << 16 | 0
    CERT_FIND_SHA1_HASH = CERT_FIND_HASH
    CERT_NAME_SIMPLE_DISPLAY_TYPE = 4
    CERT_NAME_ISSUER_FLAG = 0x1
    CRYPT_ACQUIRE_CACHE_RESOURCE_FLAG = 0x1
    CRYPT_ACQUIRE_ONLY_NCRYPT_KEY_FLAG = 0x00040000
    NCRYPT_PAD_PKCS1_FLAG = 0x2

    class CRYPT_HASH_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    class CERT_CONTEXT(ctypes.Structure):
        _fields_ = [
            ("dwCertEncodingType", wintypes.DWORD),
            ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
            ("cbCertEncoded", wintypes.DWORD),
            ("pCertInfo", ctypes.c_void_p),
            ("hCertStore", ctypes.c_void_p),
        ]

    PCERT_CONTEXT = ctypes.POINTER(CERT_CONTEXT)

    class BCRYPT_PKCS1_PADDING_INFO(ctypes.Structure):
        _fields_ = [("pszAlgId", wintypes.LPCWSTR)]

    crypt32.CertOpenStore.restype = ctypes.c_void_p
    crypt32.CertOpenStore.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    crypt32.CertEnumCertificatesInStore.restype = PCERT_CONTEXT
    crypt32.CertEnumCertificatesInStore.argtypes = [ctypes.c_void_p, PCERT_CONTEXT]
    crypt32.CertFindCertificateInStore.restype = PCERT_CONTEXT
    crypt32.CertFindCertificateInStore.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        PCERT_CONTEXT,
    ]
    crypt32.CertFreeCertificateContext.argtypes = [PCERT_CONTEXT]
    crypt32.CertCloseStore.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    crypt32.CertGetCertificateContextProperty.argtypes = [
        PCERT_CONTEXT,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
    ]
    crypt32.CertGetNameStringW.restype = wintypes.INT
    crypt32.CertGetNameStringW.argtypes = [
        PCERT_CONTEXT,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        wintypes.DWORD,
    ]
    crypt32.CryptAcquireCertificatePrivateKey.restype = wintypes.BOOL
    crypt32.CryptAcquireCertificatePrivateKey.argtypes = [
        PCERT_CONTEXT,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.BOOL),
    ]

    ncrypt.NCryptSignHash.restype = ctypes.c_long
    ncrypt.NCryptSignHash.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ubyte),
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_ubyte),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    ncrypt.NCryptFreeObject.argtypes = [ctypes.c_void_p]


def _open_my_store() -> int:
    handle = crypt32.CertOpenStore(
        ctypes.c_void_p(CERT_STORE_PROV_SYSTEM_W),
        0,
        None,
        CERT_SYSTEM_STORE_CURRENT_USER,
        ctypes.c_wchar_p("MY"),
    )
    if not handle:
        raise PdfSignerError("Could not open the Windows 'MY' certificate store.")
    return handle


def _has_private_key(cert_ctx) -> bool:
    size = wintypes.DWORD(0)
    ok = crypt32.CertGetCertificateContextProperty(
        cert_ctx, CERT_KEY_PROV_INFO_PROP_ID, None, ctypes.byref(size)
    )
    return bool(ok) and size.value > 0


def _cert_der_bytes(cert_ctx) -> bytes:
    return bytes(
        bytearray(
            ctypes.cast(
                cert_ctx.contents.pbCertEncoded,
                ctypes.POINTER(ctypes.c_ubyte * cert_ctx.contents.cbCertEncoded),
            ).contents
        )
    )


def _cert_display_name(cert_ctx, issuer: bool = False) -> str:
    flags = CERT_NAME_ISSUER_FLAG if issuer else 0
    length = crypt32.CertGetNameStringW(
        cert_ctx, CERT_NAME_SIMPLE_DISPLAY_TYPE, flags, None, None, 0
    )
    buf = ctypes.create_unicode_buffer(length)
    crypt32.CertGetNameStringW(
        cert_ctx, CERT_NAME_SIMPLE_DISPLAY_TYPE, flags, None, buf, length
    )
    return buf.value


def list_windows_certificates() -> List[CertificateInfo]:
    """Enumerate signing-capable certificates in the current user's
    Windows Personal ('MY') certificate store."""
    if sys.platform != "win32":
        raise PdfSignerError("Windows certificate store access requires Windows.")

    store = _open_my_store()
    results: List[CertificateInfo] = []
    try:
        cert_ctx = crypt32.CertEnumCertificatesInStore(store, None)
        while cert_ctx:
            if _has_private_key(cert_ctx):
                der = _cert_der_bytes(cert_ctx)
                try:
                    cert = cryptography_x509.load_der_x509_certificate(der)
                    not_after = str(cert.not_valid_after_utc)
                    thumbprint = hashlib.sha1(der).digest()
                    owner_name, owner_nif = extract_certificate_owner(cert)
                    results.append(
                        CertificateInfo(
                            thumbprint=thumbprint,
                            subject=_cert_display_name(cert_ctx),
                            issuer=_cert_display_name(cert_ctx, issuer=True),
                            not_after=not_after,
                            owner_name=owner_name,
                            owner_nif=owner_nif,
                        )
                    )
                except Exception:
                    pass
            cert_ctx = crypt32.CertEnumCertificatesInStore(store, cert_ctx)
    finally:
        crypt32.CertCloseStore(store, 0)
    return results


class WindowsStoreSigner(Signer):
    """A pyHanko Signer backed by a certificate + private key held in the
    Windows certificate store, signed via the CNG/NCrypt API so the
    private key material never leaves the OS key store."""

    def __init__(self, cert_info: CertificateInfo):
        if sys.platform != "win32":
            raise PdfSignerError("Windows certificate store access requires Windows.")
        self._thumbprint = cert_info.thumbprint

        store = _open_my_store()
        try:
            # Keep the backing buffer alive as a named variable: CRYPT_HASH_BLOB
            # only stores a raw pointer, not a reference to the array itself.
            thumb_buf = (ctypes.c_ubyte * len(cert_info.thumbprint))(
                *cert_info.thumbprint
            )
            hash_blob = CRYPT_HASH_BLOB(
                cbData=len(cert_info.thumbprint),
                pbData=ctypes.cast(thumb_buf, ctypes.POINTER(ctypes.c_ubyte)),
            )
            cert_ctx = crypt32.CertFindCertificateInStore(
                store,
                0x00000001,  # X509_ASN_ENCODING
                0,
                CERT_FIND_SHA1_HASH,
                ctypes.byref(hash_blob),
                None,
            )
            if not cert_ctx:
                raise PdfSignerError(
                    "Selected certificate could not be re-located in the store."
                )
            der = _cert_der_bytes(cert_ctx)
            signing_cert = cryptography_x509.load_der_x509_certificate(der)
            self._cert_ctx = cert_ctx
        finally:
            crypt32.CertCloseStore(store, 0)

        pyhanko_cert = _cryptography_cert_to_asn1crypto(signing_cert)
        super().__init__(signing_cert=pyhanko_cert)
        self._public_key = signing_cert.public_key()

    def __del__(self):
        cert_ctx = getattr(self, "_cert_ctx", None)
        if cert_ctx:
            crypt32.CertFreeCertificateContext(cert_ctx)

    def _acquire_key_handle(self) -> int:
        key_handle = ctypes.c_void_p()
        key_spec = wintypes.DWORD()
        caller_must_free = wintypes.BOOL()
        # Deliberately not passing CRYPT_ACQUIRE_SILENT_FLAG: PIN/password-protected
        # keys (smart cards, protected software certs) need Windows to be allowed
        # to pop up its native PIN prompt here.
        ok = crypt32.CryptAcquireCertificatePrivateKey(
            self._cert_ctx,
            CRYPT_ACQUIRE_CACHE_RESOURCE_FLAG | CRYPT_ACQUIRE_ONLY_NCRYPT_KEY_FLAG,
            None,
            ctypes.byref(key_handle),
            ctypes.byref(key_spec),
            ctypes.byref(caller_must_free),
        )
        if not ok:
            raise PdfSignerError(
                "Could not access the private key for the selected certificate "
                "(PIN entry may have been cancelled, or the key is not CNG-based)."
            )
        return key_handle, bool(caller_must_free.value)

    async def async_sign_raw(
        self, data: bytes, digest_algorithm: str, dry_run: bool = False
    ) -> bytes:
        return self.sign_raw(data, digest_algorithm)

    def sign_raw(self, data: bytes, digest_algorithm: str) -> bytes:
        digest_algorithm = digest_algorithm.lower()
        hash_fn = getattr(hashlib, digest_algorithm, None)
        if hash_fn is None:
            raise SigningError(f"Unsupported digest algorithm '{digest_algorithm}'.")
        digest = hash_fn(data).digest()

        key_handle, must_free = self._acquire_key_handle()
        try:
            if isinstance(self._public_key, rsa.RSAPublicKey):
                padding_info = BCRYPT_PKCS1_PADDING_INFO(
                    pszAlgId=digest_algorithm.upper()
                )
                flags = NCRYPT_PAD_PKCS1_FLAG
                pad_ptr = ctypes.byref(padding_info)
            elif isinstance(self._public_key, ec.EllipticCurvePublicKey):
                pad_ptr = None
                flags = 0
            else:
                raise SigningError("Only RSA and EC certificates are supported.")

            digest_buf = (ctypes.c_ubyte * len(digest))(*digest)
            result_len = wintypes.DWORD(0)
            status = ncrypt.NCryptSignHash(
                key_handle,
                pad_ptr,
                digest_buf,
                len(digest),
                None,
                0,
                ctypes.byref(result_len),
                flags,
            )
            if status != 0:
                raise SigningError(
                    f"NCryptSignHash (size query) failed: status={status:#x}"
                )

            sig_buf = (ctypes.c_ubyte * result_len.value)()
            status = ncrypt.NCryptSignHash(
                key_handle,
                pad_ptr,
                digest_buf,
                len(digest),
                sig_buf,
                result_len.value,
                ctypes.byref(result_len),
                flags,
            )
            if status != 0:
                raise SigningError(f"NCryptSignHash failed: status={status:#x}")

            raw_signature = bytes(bytearray(sig_buf))
        finally:
            if must_free:
                ncrypt.NCryptFreeObject(key_handle)

        if isinstance(self._public_key, ec.EllipticCurvePublicKey):
            half = len(raw_signature) // 2
            r = int.from_bytes(raw_signature[:half], "big")
            s = int.from_bytes(raw_signature[half:], "big")
            return encode_dss_signature(r, s)

        return raw_signature


def _cryptography_cert_to_asn1crypto(cert: cryptography_x509.Certificate):
    from asn1crypto import x509 as asn1_x509

    return asn1_x509.Certificate.load(cert.public_bytes(encoding=_der_encoding()))


def _der_encoding():
    from cryptography.hazmat.primitives.serialization import Encoding

    return Encoding.DER


# ---------------------------------------------------------------------------
# Full pipeline: stamp + sign in a single incremental update
# ---------------------------------------------------------------------------

# Zero margins + stretch-to-fit so the stamp fills the placed box exactly,
# matching what the user saw in the placement preview (StaticStampStyle's
# own default adds a 5pt margin and shrink-to-fit, which would leave an
# unwanted gap around an already aspect-matched box).
_STAMP_LAYOUT = SimpleBoxLayoutRule(
    x_align=AxisAlignment.ALIGN_MID,
    y_align=AxisAlignment.ALIGN_MID,
    margins=Margins(0, 0, 0, 0),
    inner_content_scaling=InnerScaling.STRETCH_TO_FIT,
)


def _placement_to_pdf_box(
    placement: StampPlacement, page_rect: fitz.Rect
) -> tuple[float, float, float, float]:
    """Convert a placement given in fitz's top-left-origin page space into
    the bottom-left-origin box a PDF signature field's /Rect expects.

    Note: this assumes an unrotated page (``/Rotate 0``), which covers
    every document seen so far. A rotated page's raw box doesn't simply
    match the visual one, and isn't handled here.
    """
    rect = _clamp_rect_to_page(placement.rect(), page_rect)
    return (rect.x0, page_rect.height - rect.y1, rect.x1, page_rect.height - rect.y0)


def _patched_get_single_field_annot(field: generic.DictionaryObject):
    """Replaces pyHanko's ``get_single_field_annot``, which raises unless a
    signature field has exactly one widget annotation.

    We deliberately give our signature field one widget per page (so the
    stamp shows on every page), which pyHanko's own signing step doesn't
    support out of the box -- even though pyHanko's *validation* side
    (``SigFieldCreationRule``) explicitly tolerates multiple widget kids on
    a signature field ("in principle there should be only one, but we
    don't enforce that restriction here"). The one spot that doesn't accept
    it is this appearance-lookup helper, called during signing to render
    the field's default visual appearance.

    Our fields always carry an extra invisible placeholder kid (added by
    ``prepare_sig_field`` with ``box=None``) precisely so this patched
    function has a harmless target to hand back: all of the *real*,
    visible widgets already have their appearance set beforehand, so
    whatever pyHanko renders into this placeholder is simply never seen.
    """
    kids = field.get("/Kids")
    if kids:
        return kids[0].get_object()
    return field


_cms_embedder.get_single_field_annot = _patched_get_single_field_annot


def process_pdf(
    input_path: Path,
    output_path: Path,
    stamp_image_path: Path,
    placement: StampPlacement,
    cert_info: CertificateInfo,
    reason: Optional[str] = None,
    location: Optional[str] = None,
) -> None:
    """Stamp and sign a single PDF in one incremental update, writing the
    final result only once it succeeds (the original file is never
    touched).

    The stamp image is embedded as the visible appearance of a widget on
    the new signature field itself (one widget per page), rather than
    drawn into each page's own content stream. Editing page content
    directly would go unnoticed by no one: every PDF validator (this
    app's own pyHanko included) treats that as an unauthorized change and
    flags every signature already on the document as broken, even though
    their byte ranges are technically untouched. A new signature field's
    widgets are the one kind of post-signing addition that's universally
    recognized as safe.
    """
    cleaned_bytes = _strip_leading_garbage(input_path.read_bytes())

    try:
        doc = fitz.open(stream=cleaned_bytes, filetype="pdf")
        page_rects = [doc[i].rect for i in range(doc.page_count)]
        doc.close()
    except Exception as exc:
        raise PdfSignerError(f"Could not open PDF '{input_path.name}': {exc}") from exc

    boxes = [_placement_to_pdf_box(placement, rect) for rect in page_rects]

    try:
        stamp_image = PILImage.open(stamp_image_path)
        stamp_image.load()
    except Exception as exc:
        raise PdfSignerError(
            f"Could not load stamp image '{stamp_image_path.name}': {exc}"
        ) from exc

    signer = WindowsStoreSigner(cert_info)
    writer = IncrementalPdfFileWriter(BytesIO(cleaned_bytes))
    meta = PdfSignatureMetadata(
        field_name="Signature1", reason=reason, location=location
    )
    stamp_style = StaticStampStyle(
        background=PdfImage(stamp_image),
        border_width=0,
        background_layout=_STAMP_LAYOUT,
    )

    try:
        page_ref0 = writer.find_page_for_modification(0)[0]
        _created, field_ref = prepare_sig_field(
            "Signature1",
            writer.root,
            update_writer=writer,
            existing_fields_only=False,
            box=None,  # invisible placeholder kid; see _patched_get_single_field_annot
            include_on_page=page_ref0,
            combine_annotation=False,
        )
        kids = field_ref.get_object()["/Kids"]

        for page_ix, box in enumerate(boxes):
            page_ref = writer.find_page_for_modification(page_ix)[0]
            width, height = box[2] - box[0], box[3] - box[1]
            stamp_obj = stamp_style.create_stamp(
                writer, BoxConstraints(width=width, height=height), {}
            )
            widget = generic.DictionaryObject(
                {
                    pdf_name("/Type"): pdf_name("/Annot"),
                    pdf_name("/Subtype"): pdf_name("/Widget"),
                    pdf_name("/F"): generic.NumberObject(4),  # print, not hidden
                    pdf_name("/Rect"): generic.ArrayObject(
                        [generic.FloatObject(v) for v in box]
                    ),
                    pdf_name("/P"): page_ref,
                    pdf_name("/Parent"): field_ref,
                    pdf_name("/AP"): generic.DictionaryObject(
                        {pdf_name("/N"): stamp_obj.register()}
                    ),
                }
            )
            widget_ref = writer.add_object(widget)
            kids.append(widget_ref)
            writer.register_annotation(page_ref, widget_ref)
        writer.update_container(kids)

        pdf_signer = PdfSigner(meta, signer, stamp_style=stamp_style)
        out_buffer = BytesIO()
        pdf_signer.sign_pdf(writer, existing_fields_only=True, output=out_buffer)
    except Exception as exc:
        raise PdfSignerError(f"Failed to sign '{input_path.name}': {exc}") from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(out_buffer.getvalue())


def verify_certificate_available(cert_info: Optional[CertificateInfo]) -> None:
    """Fail fast, before any file is touched, if no usable certificate
    is selected."""
    if cert_info is None:
        raise PdfSignerError(
            "No signing certificate selected. Choose a certificate from the "
            "Windows certificate store before running the batch."
        )


# Called as on_progress(index, total, phase, filename) where phase is
# "processing" or "done"; callers (e.g. the GUI) own how that is rendered
# so this module stays free of any language/presentation concerns.
ProgressCallback = Callable[[int, int, str, str], None]


def _resolve_output_path(
    output_dir: Path, stem: str, suffix: str, overwrite: bool
) -> Path:
    """Build the output path for ``stem``, appending " (1)", " (2)", ...
    until a non-colliding name is found (unless ``overwrite`` is set, in
    which case the plain name is always reused)."""
    candidate = output_dir / f"{stem}{suffix}.pdf"
    if overwrite or not candidate.exists():
        return candidate
    counter = 1
    while True:
        candidate = output_dir / f"{stem}{suffix} ({counter}).pdf"
        if not candidate.exists():
            return candidate
        counter += 1


def process_batch(
    input_paths: List[Path],
    output_dir: Path,
    stamp_image_path: Path,
    placement: StampPlacement,
    cert_info: CertificateInfo,
    suffix: str = "_signed",
    overwrite: bool = False,
    on_progress: Optional[ProgressCallback] = None,
) -> List[Path]:
    """Stamp and sign every PDF in ``input_paths``, writing results into
    ``output_dir``. Verifies signing is possible before modifying/creating
    any output file. Returns the list of produced output file paths."""
    verify_certificate_available(cert_info)
    total = len(input_paths)
    output_paths: List[Path] = []
    for index, input_path in enumerate(input_paths, start=1):
        output_path = _resolve_output_path(
            output_dir, input_path.stem, suffix, overwrite
        )
        if on_progress:
            on_progress(index, total, "processing", input_path.name)
        process_pdf(input_path, output_path, stamp_image_path, placement, cert_info)
        output_paths.append(output_path)
        if on_progress:
            on_progress(index, total, "done", output_path.name)
    return output_paths
