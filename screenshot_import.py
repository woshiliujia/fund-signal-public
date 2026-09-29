#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local-only OCR helpers for fund-detail screenshots.

Images are decoded and processed in memory.  Callers receive only selected
fields; this module never writes the image or the complete OCR transcript to
disk or the application database.
"""
import base64
import io
import re


class ScreenshotImportError(Exception):
    """A safe, user-actionable screenshot import error."""


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000


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


def _ocr(image):
    try:
        import pytesseract
        return pytesseract.image_to_string(image, lang="chi_sim+eng", config="--psm 6")
    except Exception as exc:
        raise ScreenshotImportError(
            "本机 OCR 不可用。请安装 tesseract、中文语言包和 pytesseract 后重试。"
        ) from exc


def _to_number(raw):
    if not raw:
        return None
    try:
        return float(raw.replace(",", "").replace("+", ""))
    except ValueError:
        return None


def _find_labeled_number(text, labels, percent=False):
    joined = "|".join(re.escape(label) for label in labels)
    suffix = r"\s*%" if percent else r"(?:\s*(?:元|CNY|￥|¥))?"
    pattern = re.compile(
        r"(?:%s)[^\d+\-]{0,18}([+\-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?)%s" % (joined, suffix),
        re.IGNORECASE,
    )
    match = pattern.search(text)
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


def recognise(data_url):
    image = _image_from_data_url(data_url)
    details = parse_fund_details(_ocr(image))
    warnings = [
        "OCR 结果可能把基金代码、金额或正负号识别错误；写入前请逐项确认。",
        "截图与完整 OCR 文本不会由此功能保存。",
    ]
    if not details["codes"]:
        warnings.insert(0, "未识别到 6 位基金代码，请手动填写。")
    return {"fields": details, "warnings": warnings}
