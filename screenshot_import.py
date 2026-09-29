#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local-only OCR helpers for fund-detail screenshots.

Images are decoded and processed in memory.  Callers receive only selected
fields; this module never writes the image or the complete OCR transcript to
disk or the application database.
"""
import base64
import io
import os
import re


class ScreenshotImportError(Exception):
    """A safe, user-actionable screenshot import error."""


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
_PADDLE_ENGINE = None


def _image_from_data_url(data_url):
    if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
        raise ScreenshotImportError("请选择 PNG、JPEG 或 WebP 格式的截图")
    try:
        _, encoded = data_url.split(",", 1)
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ScreenshotImportError("截图数据无法读取") from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ScreenshotImportError("截图应小于 8 MB")
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as source:
            source.verify()
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ScreenshotImportError("截图像素过大，请裁剪后重试")
            return source.convert("L")
    except ScreenshotImportError:
        raise
    except Exception as exc:
        raise ScreenshotImportError("无法识别截图格式") from exc


def _paddle_ocr_lines(image):
    """Return PaddleOCR text together with its on-screen bounding boxes."""
    global _PADDLE_ENGINE
    try:
        import numpy as np
        from paddleocr import PaddleOCR
        if _PADDLE_ENGINE is None:
            _PADDLE_ENGINE = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        result = _PADDLE_ENGINE.ocr(np.array(image.convert("RGB")), cls=True)
        lines = []
        for block in result or []:
            for item in block or []:
                if len(item) > 1 and item[1] and item[1][0]:
                    box = item[0] or []
                    if len(box) != 4:
                        continue
                    xs = [point[0] for point in box]
                    ys = [point[1] for point in box]
                    lines.append({
                        "text": str(item[1][0]),
                        "left": min(xs),
                        "right": max(xs),
                        "top": min(ys),
                        "bottom": max(ys),
                    })
        if not lines:
            raise RuntimeError("PaddleOCR 未识别出文本")
        return lines
    except Exception as exc:
        raise ScreenshotImportError("PaddleOCR 识别失败") from exc


def _paddle_ocr(image):
    """Use PaddleOCR when installed; return lines in visual reading order."""
    return "\n".join(line["text"] for line in _paddle_ocr_lines(image))


def _tesseract_ocr(image):
    try:
        import pytesseract
        from PIL import ImageEnhance
        # App screenshots often place a large amount in its own visual block.
        # Read both a normal text block and sparse text layout after enlarging
        # the image, then let the conservative parser require a nearby label.
        enlarged = image.resize((image.width * 2, image.height * 2))
        enhanced = ImageEnhance.Contrast(enlarged).enhance(1.5)
        blocks = [
            pytesseract.image_to_string(enhanced, lang="chi_sim+eng", config="--psm 6"),
            pytesseract.image_to_string(enhanced, lang="chi_sim+eng", config="--psm 11"),
        ]
        return "\n".join(block for block in blocks if block)
    except Exception as exc:
        raise ScreenshotImportError(
            "本机 OCR 不可用。请安装 tesseract、中文语言包和 pytesseract 后重试。"
        ) from exc


def _ocr(image):
    """Prefer the local high-accuracy engine, with an explicit fallback."""
    engine = os.environ.get("OCR_ENGINE", "auto").strip().lower()
    if engine not in {"auto", "paddle", "tesseract"}:
        raise ScreenshotImportError("OCR_ENGINE 仅支持 auto、paddle 或 tesseract")
    if engine in {"auto", "paddle"}:
        try:
            return _paddle_ocr(image)
        except ScreenshotImportError:
            if engine == "paddle":
                raise
    return _tesseract_ocr(image)


def _to_number(raw):
    if not raw:
        return None
    try:
        return float(raw.replace(",", "").replace("+", ""))
    except ValueError:
        return None


def _find_labeled_number(text, labels, percent=False):
    joined = "|".join(re.escape(label) for label in labels)
    suffix = r"\s*%" if percent else r"(?!\s*%)(?:\s*(?:元|CNY|￥|¥))?"
    number = r"(?<!\d)([+\-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?)(?![\d.])"
    label_before = re.compile(
        r"(?:%s)[^\d+\-]{0,24}%s%s" % (joined, number, suffix),
        re.IGNORECASE,
    )
    # Large amount cards commonly render the value above the label.  OCR may
    # preserve that visual order, so accept the inverse arrangement as well.
    label_after = re.compile(
        r"(?m)^\s*%s%s\s*\n\s*(?:%s)\s*$" % (number, suffix, joined),
        re.IGNORECASE,
    )
    # Only accept the inverse arrangement when the number occupies the line
    # immediately above a label.  A broader cross-line match would incorrectly
    # associate the previous card's amount with the next card's label.
    match = label_before.search(text) or label_after.search(text)
    return _to_number(match.group(1)) if match else None


def parse_fund_details(text):
    """Extract conservative, reviewable candidates from generic Chinese OCR."""
    codes = []
    for code in re.findall(r"(?<!\d)(\d{6})(?!\d)", text):
        if code not in codes:
            codes.append(code)
    return {
        "codes": codes[:5],
        "hold_amount": _find_labeled_number(text, ["持有金额", "持仓金额", "持有市值", "参考市值", "市值"]),
        "reported_profit": _find_labeled_number(text, ["累计收益", "持有收益", "收益金额", "昨日收益", "浮动盈亏"]),
        "reported_profit_rate": _find_labeled_number(
            text, ["累计收益率", "持有收益率", "收益率", "浮动盈亏率"], percent=True
        ),
        "cost_price": _find_labeled_number(text, ["持仓成本", "成本净值", "成本价"]),
    }


def _normalise_label(text):
    return re.sub(r"[\s()（）【】\[\]①②③]", "", text or "")


def _number_in_text(text, percent=None):
    match = re.search(r"(?<!\d)([+\-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?)(?![\d.])", text or "")
    if not match:
        return None
    if percent is True and "%" not in text:
        return None
    if percent is False and "%" in text:
        return None
    return _to_number(match.group(1))


def _value_below_label(lines, labels, percent=False):
    """Pair a card label with the number directly below it using OCR geometry."""
    normalised_labels = [_normalise_label(label) for label in labels]
    # Preserve the caller's label priority. For example, an asset-detail card
    # can show both yesterday's return and holding return; the latter is the
    # useful import field even if the former happens to be slightly closer.
    for label in normalised_labels:
        candidates = []
        for label_line in lines:
            if label not in _normalise_label(label_line["text"]):
                continue
            label_center = (label_line["left"] + label_line["right"]) / 2
            label_width = max(1, label_line["right"] - label_line["left"])
            for value_line in lines:
                value = _number_in_text(value_line["text"], percent=percent)
                if value is None:
                    continue
                vertical_gap = value_line["top"] - label_line["bottom"]
                value_center = (value_line["left"] + value_line["right"]) / 2
                horizontal_gap = abs(value_center - label_center)
                # One card's value is normally one short row below its label.
                # The horizontal guard prevents the three profit cards from
                # crossing into one another.
                if not -8 <= vertical_gap <= 260:
                    continue
                if horizontal_gap > max(180, label_width * 1.35):
                    continue
                candidates.append((vertical_gap * 2 + horizontal_gap, value))
        if candidates:
            return min(candidates)[1]
    return None


def parse_fund_layout(lines):
    """Extract fields from an OCR result that retains text positions.

    Financial apps often display several labels on one row and their values on
    the next.  Plain OCR text loses those columns; PaddleOCR boxes let us keep
    the label/value association conservative.
    """
    return {
        "hold_amount": _value_below_label(
            lines, ["持有金额", "持仓金额", "持有市值", "参考市值", "资产金额", "金额"], percent=False
        ),
        "reported_profit": _value_below_label(
            lines, ["持有收益", "累计收益", "收益金额", "昨日收益", "浮动盈亏"], percent=False
        ),
        "reported_profit_rate": _value_below_label(
            lines, ["持有收益率", "累计收益率", "收益率", "浮动盈亏率"], percent=True
        ),
        "cost_price": _value_below_label(lines, ["持仓成本", "成本净值", "成本价"], percent=False),
    }


def recognise(data_url):
    image = _image_from_data_url(data_url)
    engine = os.environ.get("OCR_ENGINE", "auto").strip().lower()
    lines = None
    if engine in {"auto", "paddle"}:
        try:
            lines = _paddle_ocr_lines(image)
            text = "\n".join(line["text"] for line in lines)
        except ScreenshotImportError:
            if engine == "paddle":
                raise
            text = _tesseract_ocr(image)
    else:
        text = _tesseract_ocr(image)
    details = parse_fund_details(text)
    if lines:
        # Geometry is more reliable for multi-column asset cards, but retain
        # text parsing for fields that do not have a confident spatial match.
        details.update({key: value for key, value in parse_fund_layout(lines).items() if value is not None})
    warnings = [
        "OCR 结果可能把基金代码、金额或正负号识别错误；写入前请逐项确认。",
        "截图与完整 OCR 文本不会由此功能保存。",
    ]
    if not details["codes"]:
        warnings.insert(0, "未识别到 6 位基金代码，请手动填写。")
    return {"fields": details, "warnings": warnings}
