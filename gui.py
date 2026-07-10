"""PyQt6 GUI for the PDF Batch Stamper & Signer.

Lets the user pick PDFs and a stamp image, click on a preview of the
first page to place the stamp, choose a signing certificate from the
Windows certificate store, and run the stamp+sign batch pipeline.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
from PyQt6.QtCore import Qt, QSize, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QIcon, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QComboBox,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import backend
import i18n

PREVIEW_MAX_WIDTH = 700

IMAGES_DIR = Path(__file__).resolve().parent / "images"
LOGO_SVG_PATH = IMAGES_DIR / "algorae-logo.svg"
LOGO_PNG_PATH = IMAGES_DIR / "diviloper.png"
DIVILOPER_URL = "https://github.com/Diviloper"
ISSUES_URL = "https://github.com/Diviloper/pdf-signer/issues"


class ClickableLabel(QLabel):
    """A QLabel that emits `clicked` on left-click, with a pointer cursor
    to signal it's interactive."""

    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class PreviewCanvas(QLabel):
    """QLabel that renders a PDF page and lets the user click to place
    the stamp, drawing a live bounding-box overlay."""

    clicked = pyqtSignal(float, float)

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(200, 200)
        self._base_pixmap: Optional[QPixmap] = None
        self._box_size_px: Optional[tuple[float, float]] = None
        self._click_pos: Optional[tuple[float, float]] = None

    def set_page_pixmap(
        self, pixmap: QPixmap, click_pos: Optional[tuple[float, float]] = None
    ) -> None:
        self._base_pixmap = pixmap
        self._click_pos = click_pos
        self.setPixmap(pixmap)
        self._redraw()

    def set_stamp_box_size(self, width_px: float, height_px: float) -> None:
        self._box_size_px = (width_px, height_px)
        self._redraw()

    def clear_page(self) -> None:
        self._base_pixmap = None
        self._click_pos = None
        self._box_size_px = None
        self.clear()

    def _pixmap_rect(self) -> Optional[tuple[float, float, int, int]]:
        """The rect the pixmap actually occupies within this widget: with
        AlignCenter, QLabel centers a pixmap smaller than the widget
        rather than scaling it, leaving a clickable margin around it."""
        if self._base_pixmap is None:
            return None
        pw, ph = self._base_pixmap.width(), self._base_pixmap.height()
        # AlignCenter centers the pixmap regardless of whether the widget is
        # larger (leaving a margin) or smaller (clipping symmetrically), so
        # the offset is not clamped to zero.
        offset_x = (self.width() - pw) / 2
        offset_y = (self.height() - ph) / 2
        return offset_x, offset_y, pw, ph

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        rect = self._pixmap_rect()
        if rect is None:
            return
        offset_x, offset_y, pw, ph = rect
        pos = event.position()
        x, y = pos.x() - offset_x, pos.y() - offset_y
        if not (0 <= x <= pw and 0 <= y <= ph):
            return  # click landed in the empty margin around the page, ignore it
        self._click_pos = (x, y)
        self._redraw()
        self.clicked.emit(x, y)

    def _redraw(self) -> None:
        if self._base_pixmap is None:
            return
        composed = QPixmap(self._base_pixmap)
        if self._click_pos and self._box_size_px:
            painter = QPainter(composed)
            pen = QPen(Qt.GlobalColor.red)
            pen.setWidth(2)
            painter.setPen(pen)
            x, y = self._click_pos
            w, h = self._box_size_px
            x = min(max(x, 0), composed.width() - w)
            y = min(max(y, 0), composed.height() - h)
            painter.drawRect(int(x), int(y), int(w), int(h))
            painter.end()
        self.setPixmap(composed)

    @property
    def click_pos(self) -> Optional[tuple[float, float]]:
        return self._click_pos


class BatchWorker(QThread):
    progress = pyqtSignal(int, int, str, str)
    finished_ok = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(
        self,
        input_paths: list[Path],
        output_dir: Path,
        stamp_image_path: Path,
        placement: "backend.StampPlacement",
        cert_info: "backend.CertificateInfo",
    ):
        super().__init__()
        self._input_paths = input_paths
        self._output_dir = output_dir
        self._stamp_image_path = stamp_image_path
        self._placement = placement
        self._cert_info = cert_info

    def run(self) -> None:
        try:
            output_paths = backend.process_batch(
                self._input_paths,
                self._output_dir,
                self._stamp_image_path,
                self._placement,
                self._cert_info,
                on_progress=lambda i, total, phase, name: self.progress.emit(
                    i, total, phase, name
                ),
            )
            self.finished_ok.emit(output_paths)
        except backend.PdfSignerError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # unexpected errors still surface to the user
            self.failed.emit(f"Unexpected error: {exc}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._lang = i18n.DEFAULT_LANGUAGE
        self.resize(1000, 750)
        if LOGO_SVG_PATH.exists():
            self.setWindowIcon(QIcon(str(LOGO_SVG_PATH)))

        self._pdf_paths: list[Path] = []
        self._stamp_image_path: Optional[Path] = None
        self._first_page_size_pt: Optional[tuple[float, float]] = None
        self._preview_zoom: float = 1.0
        self._certificates: list[backend.CertificateInfo] = []
        self._worker: Optional[BatchWorker] = None
        self._section_labels: dict[str, QLabel] = {}
        self._generated_stamp_temp_path: Optional[Path] = None
        self._stamp_pdf_point: Optional[tuple[float, float]] = None

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._on_resize_settled)

        self._build_ui()
        self._retranslate_ui()
        self._refresh_certificates()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        if self._pdf_paths:
            self._resize_timer.start(150)  # debounce so we don't re-render every pixel

    def _on_resize_settled(self) -> None:
        self._load_preview(self._pdf_paths[0], keep_placement=True)

    def _t(self, key: str, **kwargs) -> str:
        return i18n.translate(self._lang, key, **kwargs)

    # -- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        root.addWidget(self._build_left_panel(), stretch=0)
        root.addWidget(self._build_canvas_panel(), stretch=1)

    def _add_section_title(self, layout: QVBoxLayout, key: str, first: bool = False) -> None:
        if not first:
            separator = QFrame()
            separator.setFrameShape(QFrame.Shape.HLine)
            separator.setFrameShadow(QFrame.Shadow.Sunken)
            layout.addWidget(separator)
        title = QLabel()
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        self._section_labels[key] = title

    def _build_left_panel(self) -> QScrollArea:
        content = QWidget()
        layout = QVBoxLayout(content)

        lang_row = QHBoxLayout()
        self._language_label = QLabel()
        lang_row.addWidget(self._language_label)
        self._language_combo = QComboBox()
        for code, name in i18n.LANGUAGES.items():
            self._language_combo.addItem(name, userData=code)
        self._language_combo.setCurrentIndex(
            list(i18n.LANGUAGES.keys()).index(self._lang)
        )
        self._language_combo.currentIndexChanged.connect(self._on_language_changed)
        lang_row.addWidget(self._language_combo, stretch=1)
        layout.addLayout(lang_row)

        self._add_section_title(layout, "section_pdfs", first=True)
        self._pick_pdfs_btn = QPushButton()
        self._pick_pdfs_btn.clicked.connect(self._on_pick_pdfs)
        layout.addWidget(self._pick_pdfs_btn)

        self._pdf_list = QListWidget()
        self._pdf_list.setMaximumHeight(120)
        layout.addWidget(self._pdf_list)

        self._add_section_title(layout, "section_cert")
        self._cert_label = QLabel()
        layout.addWidget(self._cert_label)
        cert_row = QHBoxLayout()
        self._cert_combo = QComboBox()
        self._cert_combo.currentIndexChanged.connect(self._on_cert_changed)
        cert_row.addWidget(self._cert_combo, stretch=1)
        self._refresh_btn = QPushButton()
        self._refresh_btn.clicked.connect(self._refresh_certificates)
        cert_row.addWidget(self._refresh_btn)
        layout.addLayout(cert_row)

        self._add_section_title(layout, "section_stamp")
        self._mode_auto_radio = QRadioButton()
        self._mode_file_radio = QRadioButton()
        self._mode_auto_radio.setChecked(True)
        self._stamp_mode_group = QButtonGroup(self)
        self._stamp_mode_group.addButton(self._mode_auto_radio)
        self._stamp_mode_group.addButton(self._mode_file_radio)
        self._mode_auto_radio.toggled.connect(self._on_stamp_mode_changed)
        layout.addWidget(self._mode_auto_radio)
        layout.addWidget(self._mode_file_radio)

        self._signer_name_edit = QLineEdit()
        layout.addWidget(self._signer_name_edit)

        self._pick_stamp_btn = QPushButton()
        self._pick_stamp_btn.clicked.connect(self._on_pick_stamp)
        layout.addWidget(self._pick_stamp_btn)

        self._stamp_label = QLabel()
        self._stamp_label.setWordWrap(True)
        layout.addWidget(self._stamp_label)

        self._stamp_width_label = QLabel()
        layout.addWidget(self._stamp_width_label)
        self._stamp_width_spin = QDoubleSpinBox()
        self._stamp_width_spin.setRange(10, 1000)
        self._stamp_width_spin.setValue(150)
        self._stamp_width_spin.valueChanged.connect(self._update_stamp_box_preview)
        layout.addWidget(self._stamp_width_spin)

        self._add_section_title(layout, "section_output")
        self._pick_output_btn = QPushButton()
        self._pick_output_btn.clicked.connect(self._on_pick_output_dir)
        layout.addWidget(self._pick_output_btn)
        self._output_dir_label = QLabel()
        self._output_dir_label.setWordWrap(True)
        layout.addWidget(self._output_dir_label)
        self._output_dir: Optional[Path] = None

        self._add_section_title(layout, "section_run")
        self._run_btn = QPushButton()
        self._run_btn.clicked.connect(self._on_run)
        layout.addWidget(self._run_btn)

        self._progress_bar = QProgressBar()
        layout.addWidget(self._progress_bar)

        self._status_label = QLabel()
        layout.addWidget(self._status_label)
        self._status_log = QPlainTextEdit()
        self._status_log.setReadOnly(True)
        layout.addWidget(self._status_log, stretch=1)

        self._bug_report_label = ClickableLabel()
        self._bug_report_label.setText("🐛")
        bug_font = self._bug_report_label.font()
        bug_font.setPointSize(bug_font.pointSize() + 6)
        self._bug_report_label.setFont(bug_font)
        self._bug_report_label.clicked.connect(self._on_bug_report_clicked)

        self._logo_label = ClickableLabel()
        self._logo_label.setToolTip(DIVILOPER_URL)
        self._logo_label.clicked.connect(self._on_logo_clicked)
        if LOGO_PNG_PATH.exists():
            logo_pixmap = QPixmap(str(LOGO_PNG_PATH))
            if not logo_pixmap.isNull():
                # Scale in physical pixels (logical width * screen DPR) and tag
                # the result with that DPR, otherwise the pixmap is rendered at
                # its logical size in physical pixels and gets blurrily upscaled
                # by Qt on any HiDPI display (e.g. Windows' 125%/150% scaling).
                screen = self.screen() or QApplication.primaryScreen()
                dpr = screen.devicePixelRatio() if screen else 1.0
                target_width_pt = 220
                scaled = logo_pixmap.scaledToWidth(
                    int(target_width_pt * dpr), Qt.TransformationMode.SmoothTransformation
                )
                scaled.setDevicePixelRatio(dpr)
                self._logo_label.setPixmap(scaled)
        self._logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        footer_row = QHBoxLayout()
        footer_row.setSpacing(0)
        footer_row.addWidget(self._bug_report_label)
        footer_row.addStretch(1)
        footer_row.addWidget(self._logo_label)
        footer_row.addStretch(1)
        # Mirror the bug icon's width on the right so the two stretches
        # above are symmetric and the logo lands exactly centered.
        footer_row.addSpacing(self._bug_report_label.sizeHint().width())
        layout.addLayout(footer_row)

        self._on_stamp_mode_changed()  # sync widget enabled-state with the default mode

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        scroll.setMinimumWidth(380)
        return scroll

    def _build_canvas_panel(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        self._canvas_hint_label = QLabel()
        layout.addWidget(self._canvas_hint_label)
        self._canvas = PreviewCanvas()
        self._canvas.clicked.connect(self._on_canvas_clicked)
        layout.addWidget(self._canvas, stretch=1)
        return container

    def _retranslate_ui(self) -> None:
        self.setWindowTitle(self._t("window_title"))
        self._language_label.setText(self._t("language_label"))
        self._pick_pdfs_btn.setText(self._t("select_pdfs_btn"))
        self._mode_file_radio.setText(self._t("stamp_mode_file"))
        self._mode_auto_radio.setText(self._t("stamp_mode_auto"))
        self._pick_stamp_btn.setText(self._t("select_stamp_btn"))
        self._stamp_label.setText(
            self._t("stamp_selected", name=self._stamp_image_path.name)
            if self._stamp_image_path
            else self._t("no_stamp_selected")
        )
        self._signer_name_edit.setPlaceholderText(self._t("signer_name_placeholder"))
        self._stamp_width_label.setText(self._t("stamp_width_label"))
        self._cert_label.setText(self._t("cert_label"))
        self._refresh_btn.setText(self._t("refresh_btn"))
        self._pick_output_btn.setText(self._t("select_output_btn"))
        self._output_dir_label.setText(
            self._t("output_selected", path=str(self._output_dir))
            if self._output_dir
            else self._t("no_output_selected")
        )
        self._run_btn.setText(self._t("run_btn"))
        self._status_label.setText(self._t("status_label"))
        self._canvas_hint_label.setText(self._t("canvas_hint"))
        self._bug_report_label.setToolTip(self._t("bug_report_tooltip"))
        for key, label in self._section_labels.items():
            label.setText(self._t(key))

    # -- Event handlers -----------------------------------------------------

    def _on_language_changed(self) -> None:
        self._lang = self._language_combo.currentData()
        self._retranslate_ui()

    def _on_logo_clicked(self) -> None:
        QDesktopServices.openUrl(QUrl(DIVILOPER_URL))

    def _on_bug_report_clicked(self) -> None:
        QDesktopServices.openUrl(QUrl(ISSUES_URL))

    def _on_pick_pdfs(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            self._t("file_dialog_select_pdfs"),
            str(_default_pdf_start_dir()),
            self._t("file_dialog_pdf_filter"),
        )
        if not files:
            return

        was_empty = not self._pdf_paths
        existing = {p.resolve() for p in self._pdf_paths}
        for f in files:
            path = Path(f)
            if path.resolve() not in existing:
                self._pdf_paths.append(path)
                existing.add(path.resolve())
        self._rebuild_pdf_list()

        if was_empty and self._pdf_paths:
            self._load_preview(self._pdf_paths[0])
            self._output_dir = self._pdf_paths[0].parent
            self._output_dir_label.setText(
                self._t("output_selected", path=str(self._output_dir))
            )

    def _rebuild_pdf_list(self) -> None:
        self._pdf_list.clear()
        for path in self._pdf_paths:
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 26))
            self._pdf_list.addItem(item)

            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 0, 4, 0)
            label = QLabel(path.name)
            label.setToolTip(str(path))
            row_layout.addWidget(label, stretch=1)

            remove_btn = QToolButton()
            remove_btn.setText("✕")
            remove_btn.setAutoRaise(True)
            remove_btn.setFixedSize(20, 20)
            remove_btn.setToolTip(self._t("remove_pdf_tooltip"))
            remove_btn.clicked.connect(lambda checked=False, p=path: self._on_remove_pdf(p))
            row_layout.addWidget(remove_btn)

            self._pdf_list.setItemWidget(item, row)

    def _on_remove_pdf(self, path: Path) -> None:
        if path not in self._pdf_paths:
            return
        was_first = self._pdf_paths[0] == path
        self._pdf_paths.remove(path)

        if not self._pdf_paths:
            self._clear_pdf_selection()
            return

        self._rebuild_pdf_list()
        if was_first:
            # The reference page for the stamp placement just changed;
            # reset the placement rather than risk it landing somewhere wrong.
            self._load_preview(self._pdf_paths[0])

    def _clear_pdf_selection(self) -> None:
        self._pdf_paths = []
        self._rebuild_pdf_list()
        self._first_page_size_pt = None
        self._stamp_pdf_point = None
        self._canvas.clear_page()

    def _on_pick_stamp(self) -> None:
        file, _ = QFileDialog.getOpenFileName(
            self,
            self._t("file_dialog_select_stamp"),
            "",
            self._t("file_dialog_image_filter"),
        )
        if not file:
            return
        self._stamp_image_path = Path(file)
        self._stamp_label.setText(self._t("stamp_selected", name=self._stamp_image_path.name))
        self._update_stamp_box_preview()

    def _on_pick_output_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, self._t("file_dialog_select_output"))
        if not directory:
            return
        self._output_dir = Path(directory)
        self._output_dir_label.setText(self._t("output_selected", path=str(self._output_dir)))

    def _on_canvas_clicked(self, x: float, y: float) -> None:
        if self._first_page_size_pt is None:
            return
        pdf_w, pdf_h = self._first_page_size_pt
        canvas_w = self._canvas.pixmap().width()
        canvas_h = self._canvas.pixmap().height()
        self._stamp_pdf_point = backend.gui_point_to_pdf_point(
            x, y, canvas_w, canvas_h, pdf_w, pdf_h
        )
        self._log(self._t("stamp_position_set_log"))

    def _on_stamp_mode_changed(self) -> None:
        auto = self._is_auto_mode()
        self._pick_stamp_btn.setEnabled(not auto)
        self._signer_name_edit.setEnabled(auto)
        self._update_stamp_box_preview()

    def _on_run(self) -> None:
        try:
            self._validate_before_run()
        except backend.PdfSignerError as exc:
            QMessageBox.warning(self, self._t("cannot_start_title"), str(exc))
            return

        stamp_image_path = self._stamp_image_path
        if self._is_auto_mode():
            stamp_image_path = Path(tempfile.gettempdir()) / "pdf_signer_generated_stamp.png"
            backend.write_text_stamp_image(
                stamp_image_path, self._signer_name_edit.text(), datetime.now()
            )
            self._generated_stamp_temp_path = stamp_image_path

        placement = self._current_placement()
        cert_info = self._certificates[self._cert_combo.currentIndex()]

        self._progress_bar.setMaximum(len(self._pdf_paths))
        self._progress_bar.setValue(0)
        self._worker = BatchWorker(
            self._pdf_paths, self._output_dir, stamp_image_path, placement, cert_info
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    # -- Validation / helpers ------------------------------------------------

    def _validate_before_run(self) -> None:
        if not self._pdf_paths:
            raise backend.PdfSignerError(self._t("validation_no_pdfs"))
        if self._is_auto_mode():
            if not self._signer_name_edit.text().strip():
                raise backend.PdfSignerError(self._t("validation_no_name"))
        elif not self._stamp_image_path:
            raise backend.PdfSignerError(self._t("validation_no_stamp_image"))
        if self._canvas.click_pos is None or self._first_page_size_pt is None:
            raise backend.PdfSignerError(self._t("validation_no_click"))
        if not self._output_dir:
            raise backend.PdfSignerError(self._t("validation_no_output_dir"))
        if not self._certificates or self._cert_combo.currentIndex() < 0:
            raise backend.PdfSignerError(self._t("validation_no_cert"))

    def _is_auto_mode(self) -> bool:
        return self._mode_auto_radio.isChecked()

    def _stamp_box_pixel_size(self) -> tuple[int, int]:
        if self._is_auto_mode():
            return (
                int(backend.TEXT_STAMP_WIDTH_PT * backend.TEXT_STAMP_ZOOM),
                int(backend.TEXT_STAMP_HEIGHT_PT * backend.TEXT_STAMP_ZOOM),
            )
        if not self._stamp_image_path:
            return (0, 0)
        return _image_pixel_size(self._stamp_image_path)

    def _current_placement(self) -> "backend.StampPlacement":
        click_x, click_y = self._canvas.click_pos
        pdf_w, pdf_h = self._first_page_size_pt
        canvas_w = self._canvas.pixmap().width()
        canvas_h = self._canvas.pixmap().height()
        img_w_px, img_h_px = self._stamp_box_pixel_size()
        return backend.stamp_placement_for_click(
            click_x,
            click_y,
            canvas_w,
            canvas_h,
            pdf_w,
            pdf_h,
            self._stamp_width_spin.value(),
            (img_w_px, img_h_px),
        )

    def _update_stamp_box_preview(self) -> None:
        if self._first_page_size_pt is None:
            return
        img_w_px, img_h_px = self._stamp_box_pixel_size()
        pdf_w, _ = self._first_page_size_pt
        canvas_w = self._canvas.pixmap().width() if self._canvas.pixmap() else 0
        if not canvas_w or img_w_px <= 0:
            return
        stamp_width_pt = self._stamp_width_spin.value()
        stamp_width_canvas_px = stamp_width_pt / (pdf_w / canvas_w)
        aspect = img_h_px / img_w_px
        self._canvas.set_stamp_box_size(stamp_width_canvas_px, stamp_width_canvas_px * aspect)

    def _load_preview(self, pdf_path: Path, keep_placement: bool = False) -> None:
        try:
            doc = fitz.open(str(pdf_path))
            page = doc[0]
        except Exception as exc:
            QMessageBox.warning(self, self._t("could_not_open_pdf_title"), f"{pdf_path.name}: {exc}")
            return

        if not keep_placement:
            self._stamp_pdf_point = None

        self._first_page_size_pt = (page.rect.width, page.rect.height)
        # Fit the page to the canvas's actual current size (both dimensions),
        # not just a fixed width -- otherwise a page whose aspect ratio
        # doesn't match the panel gets clipped to its center by the QLabel.
        # Fall back to a fixed guess only if the widget isn't laid out yet.
        available_w = self._canvas.width() if self._canvas.width() > 100 else PREVIEW_MAX_WIDTH
        available_h = self._canvas.height() if self._canvas.height() > 100 else PREVIEW_MAX_WIDTH
        zoom = min(available_w / page.rect.width, available_h / page.rect.height, 2.0)
        self._preview_zoom = zoom
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)

        click_pos = None
        if self._stamp_pdf_point is not None:
            pdf_x, pdf_y = self._stamp_pdf_point
            click_pos = backend.pdf_point_to_gui_point(
                pdf_x, pdf_y, pix.width, pix.height, page.rect.width, page.rect.height
            )

        self._canvas.set_page_pixmap(QPixmap.fromImage(image), click_pos=click_pos)
        doc.close()
        self._update_stamp_box_preview()

    def _refresh_certificates(self) -> None:
        self._cert_combo.clear()
        try:
            self._certificates = backend.list_windows_certificates()
        except backend.PdfSignerError as exc:
            self._certificates = []
            self._log(self._t("cert_lookup_failed_log", message=str(exc)))
            self._on_cert_changed(-1)
            return
        if not self._certificates:
            self._log(self._t("no_certs_found_log"))
            self._on_cert_changed(-1)
            return
        self._cert_combo.addItems(str(c) for c in self._certificates)
        self._on_cert_changed(self._cert_combo.currentIndex())

    def _on_cert_changed(self, index: int) -> None:
        """Extracted from the certificate's Subject (name, and NIF/NIE/DNI
        for Spanish qualified certs) and pre-fill + lock the signer-name
        field with it, so the visible stamp always matches who actually
        signed. Falls back to a free-text field when nothing usable could
        be extracted from the certificate."""
        cert = self._certificates[index] if 0 <= index < len(self._certificates) else None
        if cert and cert.owner_name:
            display = cert.owner_name
            if cert.owner_nif:
                display = f"{display} ({cert.owner_nif})"
            self._signer_name_edit.setText(display)
            self._signer_name_edit.setReadOnly(True)
        elif self._signer_name_edit.isReadOnly():
            self._signer_name_edit.clear()
            self._signer_name_edit.setReadOnly(False)

    def _on_progress(self, index: int, total: int, phase: str, name: str) -> None:
        self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(index)
        key = "processing_log" if phase == "processing" else "done_log"
        self._log(self._t(key, name=name))

    def _on_finished_ok(self, output_paths: list) -> None:
        self._cleanup_generated_stamp()
        self._log(self._t("batch_complete_log"))
        self._show_finished_dialog(output_paths)

    def _on_failed(self, message: str) -> None:
        self._cleanup_generated_stamp()
        self._log(self._t("error_log_prefix", message=message))
        QMessageBox.critical(self, self._t("processing_failed_title"), message)

    def _show_finished_dialog(self, output_paths: list[Path]) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(self._t("done_title"))
        box.setText(self._t("done_message"))

        sign_more_btn = box.addButton(self._t("sign_more_btn"), QMessageBox.ButtonRole.ActionRole)
        open_docs_btn = box.addButton(self._t("open_documents_btn"), QMessageBox.ButtonRole.ActionRole)
        open_folder_btn = box.addButton(self._t("open_folder_btn"), QMessageBox.ButtonRole.ActionRole)
        close_btn = box.addButton(self._t("close_app_btn"), QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(sign_more_btn)
        box.exec()

        clicked = box.clickedButton()
        if clicked is sign_more_btn:
            self._clear_pdf_selection()
        elif clicked is open_docs_btn:
            for path in output_paths:
                self._open_file(path)
        elif clicked is open_folder_btn:
            self._open_folder(self._output_dir)
        elif clicked is close_btn:
            self.close()

    def _open_file(self, path: Path) -> None:
        try:
            os.startfile(str(path))  # noqa: S606 (Windows-only tool by design)
        except OSError as exc:
            self._log(self._t("error_log_prefix", message=f"{path.name}: {exc}"))

    def _open_folder(self, path: Optional[Path]) -> None:
        if path is None:
            return
        self._open_file(path)

    def _cleanup_generated_stamp(self) -> None:
        if self._generated_stamp_temp_path and self._generated_stamp_temp_path.exists():
            self._generated_stamp_temp_path.unlink()
        self._generated_stamp_temp_path = None

    def _log(self, message: str) -> None:
        self._status_log.appendPlainText(message)


def _image_pixel_size(path: Path) -> tuple[int, int]:
    pix = QPixmap(str(path))
    return pix.width(), pix.height()


def _default_pdf_start_dir() -> Path:
    """Prefer Downloads, then Documents, then the home directory."""
    home = Path.home()
    for candidate in ("Downloads", "Documents"):
        directory = home / candidate
        if directory.is_dir():
            return directory
    return home


def run() -> None:
    app = QApplication([])
    window = MainWindow()
    window.showMaximized()
    app.exec()
