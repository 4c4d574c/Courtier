"""Tests for preprocessor temp directory registration and safe cleanup."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from docparse.parsers.scanned.preprocessor import (
    _TEMP_DIRS,
    cleanup_temp_images,
    deskew_image,
    pdf_to_images,
    prepare_images,
    resize_images_for_ocr,
)
from PIL import Image as PILImage


@pytest.fixture(autouse=True)
def _isolate_temp_registry():
    """Snapshot the module registry and purge test-created temp dirs."""
    saved = set(_TEMP_DIRS)
    yield
    for d in _TEMP_DIRS - saved:
        shutil.rmtree(d, ignore_errors=True)
    _TEMP_DIRS.clear()
    _TEMP_DIRS.update(saved)


def _make_png(path: Path, size: tuple[int, int] = (100, 80)) -> str:
    img = PILImage.new("RGB", size, color=(255, 255, 255))
    img.save(path)
    img.close()
    return str(path)


def _make_pdf(path: Path, num_pages: int = 2) -> str:
    import fitz

    doc = fitz.open()
    for _ in range(num_pages):
        doc.new_page()
    doc.save(path)
    doc.close()
    return str(path)


class TestPrepareImages:
    def test_image_input_registers_nothing(self, tmp_path):
        png = _make_png(tmp_path / "scan.png")
        before = set(_TEMP_DIRS)

        assert prepare_images(png) == [png]
        assert set(_TEMP_DIRS) == before

    def test_unsupported_type_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unsupported file type"):
            prepare_images(str(tmp_path / "doc.txt"))


class TestCleanupSafety:
    def test_cleanup_preserves_user_dir_with_docparse_substring(self, tmp_path):
        # Bait: the old substring heuristic would rmtree this user directory
        user_dir = tmp_path / "docparse_user_scans"
        user_dir.mkdir()
        png = _make_png(user_dir / "page1.png")

        prepare_images(png)
        cleanup_temp_images([png])

        assert Path(png).exists()
        assert user_dir.exists()

    def test_cleanup_ignores_unregistered_dirs(self, tmp_path):
        other_dir = tmp_path / "docparse_anything"
        other_dir.mkdir()
        png = _make_png(other_dir / "img.png")

        cleanup_temp_images([png])

        assert Path(png).exists()


class TestPdfToImages:
    def test_registers_and_cleanup_removes(self, tmp_path):
        pdf = _make_pdf(tmp_path / "scan.pdf", num_pages=2)

        paths = pdf_to_images(pdf)

        assert len(paths) == 2
        for p in paths:
            assert Path(p).exists()
            assert str(Path(p).parent) in _TEMP_DIRS
            # Each page is rendered so its long side meets the 2048px
            # target — well above a fixed 150 DPI render (~1754px for A4).
            with PILImage.open(p) as img:
                assert 1900 <= max(img.size) <= 2048

        cleanup_temp_images(paths)

        for p in paths:
            assert not Path(p).parent.exists()
            assert str(Path(p).parent) not in _TEMP_DIRS

    def test_rendered_pages_pass_through_resize(self, tmp_path):
        """PDF pages rendered at the target long side need no resize copy."""
        pdf = _make_pdf(tmp_path / "scan.pdf", num_pages=1)

        paths = pdf_to_images(pdf, target_long_side=2048)
        before = set(_TEMP_DIRS)

        out = resize_images_for_ocr(paths, 2048)

        assert out == paths
        assert set(_TEMP_DIRS) == before

    def test_page_indices_renders_subset(self, tmp_path):
        """page_indices 只渲染指定页，输出顺序与给定页码一致。"""
        pdf = _make_pdf(tmp_path / "scan.pdf", num_pages=3)

        paths = pdf_to_images(pdf, page_indices=[0, 2])

        assert len(paths) == 2
        assert [Path(p).name for p in paths] == ["page_0.png", "page_2.png"]
        for p in paths:
            assert Path(p).exists()

        cleanup_temp_images(paths)

    def test_page_indices_none_renders_all(self, tmp_path):
        pdf = _make_pdf(tmp_path / "scan.pdf", num_pages=3)

        paths = pdf_to_images(pdf, page_indices=None)

        assert [Path(p).name for p in paths] == ["page_0.png", "page_1.png", "page_2.png"]

        cleanup_temp_images(paths)


class TestDeskewImage:
    def test_no_cv2_returns_original(self, tmp_path, monkeypatch):
        """cv2 不可导入 → no-op 返回原路径，不注册临时目录。"""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "cv2":
                raise ImportError("No module named 'cv2'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        png = _make_png(tmp_path / "scan.png")
        before = set(_TEMP_DIRS)

        assert deskew_image(png) == png
        assert set(_TEMP_DIRS) == before

    def test_deskew_corrects_rotated_text(self, tmp_path):
        """合成 5° 歪斜文本行图像，校正后残余倾角 < 1°。"""
        cv2 = pytest.importorskip("cv2")
        import numpy as np

        # 白底黑条模拟文本行
        img = np.full((400, 600, 3), 255, dtype=np.uint8)
        for y in range(80, 320, 40):
            cv2.rectangle(img, (80, y), (520, y + 12), (0, 0, 0), -1)
        matrix = cv2.getRotationMatrix2D((300, 200), 5.0, 1.0)
        rotated = cv2.warpAffine(img, matrix, (600, 400), borderMode=cv2.BORDER_REPLICATE)
        skewed = tmp_path / "skewed.png"
        cv2.imwrite(str(skewed), rotated)

        before = set(_TEMP_DIRS)
        out = deskew_image(str(skewed))

        try:
            assert out != str(skewed)
            assert str(Path(out).parent) in _TEMP_DIRS - before

            # 与实现相同的估计法复测输出：残余应 ≈ 0
            corrected = cv2.imread(out)
            gray = cv2.cvtColor(corrected, cv2.COLOR_BGR2GRAY)
            binary = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15
            )
            coords = np.column_stack(np.where(binary > 0))
            angle = cv2.minAreaRect(coords)[-1]
            if angle > 45:
                angle -= 90
            residual = -(90 + angle) if angle < -45 else -angle
            assert abs(residual) < 1.0
        finally:
            cleanup_temp_images([out])


class TestResizeImagesForOcr:
    def test_resize_registers_temp_copy_and_cleanup_removes(self, tmp_path):
        big = _make_png(tmp_path / "big.png", size=(3000, 2000))

        resized = resize_images_for_ocr([big], max_long_side=2048)

        assert resized[0] != big
        assert Path(resized[0]).exists()
        assert str(Path(resized[0]).parent) in _TEMP_DIRS

        cleanup_temp_images(resized)

        assert not Path(resized[0]).parent.exists()
        assert Path(big).exists()  # user original untouched

    def test_small_image_passes_through(self, tmp_path):
        small = _make_png(tmp_path / "small.png", size=(800, 600))
        before = set(_TEMP_DIRS)

        out = resize_images_for_ocr([small], max_long_side=2048)

        assert out == [small]
        assert set(_TEMP_DIRS) == before

    def test_exception_leaves_created_dirs_registered(self, tmp_path):
        big = _make_png(tmp_path / "big.png", size=(3000, 2000))
        corrupt = tmp_path / "corrupt.png"
        corrupt.write_bytes(b"not a real png")
        before = set(_TEMP_DIRS)

        with pytest.raises(Exception):
            resize_images_for_ocr([big, str(corrupt)], max_long_side=2048)

        # The temp dir created before the failure stays registered so a
        # caller-side finally/cleanup can still remove it.
        new_dirs = set(_TEMP_DIRS) - before
        assert len(new_dirs) == 1
        temp_dir = next(iter(new_dirs))
        assert Path(temp_dir).exists()

        cleanup_temp_images([os.path.join(temp_dir, "big.png")])

        assert not Path(temp_dir).exists()
        assert temp_dir not in _TEMP_DIRS
