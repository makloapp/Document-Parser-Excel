#!/usr/bin/env python3
"""
OCR spracovanie dokladov – webový wrapper.
Použitie: python3 ocr_process.py <vstupny_subor> <excel_vystup>
Výstup: JSON na stdout s extrahovanými dátami
"""
from pathlib import Path
from collections import Counter
import json
import re
import sys
import time
import cv2
import fitz
import numpy as np
import pandas as pd
import pytesseract

CONFIG = {
    "ocr_language": "slk+eng",
    "ocr_fallback_language": "eng",
    "tesseract_timeout_seconds": 30,
    "faint_scan_mode": "auto",
    "debug_enabled": False,
    "write_ocr_text_sheet": True,
    "validation_mode": "normal",
    "check_tolerance": 0.002,
    "rate_check_tolerance": 0.02,
    "timing_enabled": False,
    "ocr_fast_first": True,
    "ocr_fast_accept_min_score": 70,
    "ocr_fast_accept_min_money_count": 2,
    "ocr_fast_accept_require_date_or_total_keyword": True,
    "ocr_progressive_stop": True,
    "ocr_faint_max_calls": 5,
    "ocr_max_long_side": 2200,
    "ocr_min_short_side": 900,
    "pdf_dpi": 300,
}

SUPPORTED_EXTENSIONS = [".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"]


def pdf_to_images(pdf_path: Path, dpi: int = 300):
    doc = fitz.open(pdf_path)
    images = []
    for page_index in range(len(doc)):
        page = doc[page_index]
        zoom = dpi / 72
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            img = cv2.cvtColor(img, cv2.COLOR_RGBA2RGB)
        images.append((page_index + 1, img))
    return images


def load_input_file(file_path: Path):
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return pdf_to_images(file_path)
    if suffix in [".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"]:
        img = cv2.imread(str(file_path))
        if img is None:
            raise ValueError(f"Nepodarilo sa načítať obrázok: {file_path}")
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        return [(1, img)]
    raise ValueError(f"Nepodporovaný typ súboru: {suffix}")


def _find_intervals(active, min_width):
    intervals = []
    start = None
    for i, value in enumerate(active):
        if value and start is None:
            start = i
        if (not value or i == len(active) - 1) and start is not None:
            end = i - 1 if not value else i
            if end - start + 1 >= min_width:
                intervals.append((start, end))
            start = None
    return intervals


def _projection_boxes(gray, dark_limit=180, active_threshold=0.05, smooth_frac=0.01, min_width_frac=0.12):
    h, w = gray.shape
    mask = (gray < dark_limit).astype(np.uint8)
    col_projection = mask.mean(axis=0)
    kernel_size = max(31, int(w * smooth_frac))
    if kernel_size % 2 == 0:
        kernel_size += 1
    smooth = np.convolve(col_projection, np.ones(kernel_size) / kernel_size, mode="same")
    min_width = int(w * min_width_frac)
    return _find_intervals(smooth > active_threshold, min_width=min_width), mask


def _score_intervals(intervals, page_width):
    if not intervals:
        return -10_000
    count = len(intervals)
    widths = [x2 - x1 + 1 for x1, x2 in intervals]
    total_width = sum(widths)
    coverage = total_width / page_width
    max_width = max(widths) / page_width
    score = 0
    if 2 <= count <= 12:
        score += 1000 + count * 50
    elif count == 1:
        score += 100
    else:
        score -= 200
    if count == 1 and max_width > 0.85:
        score -= 700
    if coverage > 0.95:
        score -= 500
    if any(w / page_width < 0.08 for w in widths):
        score -= 300
    return score


def _merge_close_intervals(intervals, max_gap):
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start - merged[-1][1] <= max_gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def _split_box_by_seams(page_img, box):
    x1, y1, x2, y2 = box
    crop = page_img[y1:y2, x1:x2]
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    if w < 300 or h < 300:
        return [box]
    y_top = int(h * 0.12)
    y_bottom = int(h * 0.88)
    band = gray[y_top:y_bottom, :]
    col_mean = band.mean(axis=0)
    k = max(31, int(w * 0.025))
    if k % 2 == 0:
        k += 1
    smooth_mean = np.convolve(col_mean, np.ones(k) / k, mode="same")
    median_val = float(np.median(smooth_mean))
    dark_limit = median_val - 8.0
    dark_active = smooth_mean < dark_limit
    ink = (band < 175).astype(np.uint8).mean(axis=0)
    smooth_ink = np.convolve(ink, np.ones(k) / k, mode="same")
    ink_limit = max(0.015, float(np.percentile(smooth_ink, 12)))
    light_gap_active = smooth_ink < ink_limit
    candidates = []
    min_gap_width = max(8, int(w * 0.008))
    for active, typ in [(dark_active, "dark"), (light_gap_active, "light")]:
        intervals = _find_intervals(active, min_width=min_gap_width)
        for a, b in intervals:
            center = (a + b) // 2
            if center < w * 0.18 or center > w * 0.82:
                continue
            left_mean = float(np.mean(smooth_mean[max(0, center - 90):max(1, center - 30)]))
            right_mean = float(np.mean(smooth_mean[min(w, center + 30):min(w, center + 90)]))
            seam_mean = float(np.mean(smooth_mean[a:b + 1]))
            contrast = ((left_mean + right_mean) / 2.0) - seam_mean
            candidates.append((contrast, a, b, center, typ))
    split_points = []
    for contrast, a, b, center, typ in sorted(candidates, reverse=True):
        if typ == "dark" and contrast < 6.0:
            continue
        if typ == "light" and smooth_ink[center] > 0.025:
            continue
        if any(abs(center - p) < w * 0.07 for p in split_points):
            continue
        split_points.append(center)
    split_points = sorted(split_points)
    if not split_points:
        return [box]
    points = [0] + split_points + [w]
    min_part_width = int(w * 0.22)
    parts = []
    for a, b in zip(points[:-1], points[1:]):
        if b - a >= min_part_width:
            parts.append((x1 + a, y1, x1 + b, y2))
    return parts if len(parts) >= 2 else [box]


def _paper_region_boxes(page_img):
    gray = cv2.cvtColor(page_img, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    candidate_boxes = []
    for threshold in [205, 198, 190, 180]:
        mask = (gray > threshold).astype(np.uint8) * 255
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, w // 90), max(9, h // 120)))
        clean = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
        clean = cv2.morphologyEx(clean, cv2.MORPH_OPEN, kernel, iterations=1)
        contours, _ = cv2.findContours(clean, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        for c in contours:
            x, y, bw, bh = cv2.boundingRect(c)
            area = bw * bh
            if area < w * h * 0.07:
                continue
            if bw < w * 0.18 or bh < h * 0.25:
                continue
            if bw > w * 0.92 and bh > h * 0.80:
                continue
            pad_x = int(bw * 0.025)
            pad_y = int(bh * 0.025)
            boxes.append((max(0, x - pad_x), max(0, y - pad_y), min(w, x + bw + pad_x), min(h, y + bh + pad_y)))
        if len(boxes) >= 2:
            candidate_boxes.extend(boxes)
            break
    if not candidate_boxes:
        return []
    filtered = []
    for box in candidate_boxes:
        x1, y1, x2, y2 = box
        duplicate = False
        for fx1, fy1, fx2, fy2 in filtered:
            inter_x1, inter_y1 = max(x1, fx1), max(y1, fy1)
            inter_x2, inter_y2 = min(x2, fx2), min(y2, fy2)
            inter = max(0, inter_x2 - inter_x1) * max(0, inter_y2 - inter_y1)
            area = (x2 - x1) * (y2 - y1)
            farea = (fx2 - fx1) * (fy2 - fy1)
            if inter / max(1, min(area, farea)) > 0.70:
                duplicate = True
                break
        if not duplicate:
            filtered.append(box)
    return filtered


def _boxes_score(boxes, page_w, page_h):
    if not boxes:
        return -10000
    count = len(boxes)
    widths = [(x2 - x1) / page_w for x1, y1, x2, y2 in boxes]
    heights = [(y2 - y1) / page_h for x1, y1, x2, y2 in boxes]
    score = 0
    if 2 <= count <= 12:
        score += 1200 + 80 * count
    elif count == 1:
        score += 100
    else:
        score -= 300
    if count == 1 and widths[0] > 0.85:
        score -= 800
    if any(w < 0.12 for w in widths):
        score -= 250
    if any(h < 0.20 for h in heights):
        score -= 150
    return score


def _trim_box_to_bright_paper(page_img, box):
    x1, y1, x2, y2 = box
    crop = page_img[y1:y2, x1:x2]
    if crop.size == 0:
        return box
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    best_active = None
    for threshold in [200, 190, 180]:
        bright_frac = (gray > threshold).mean(axis=1)
        k = max(21, int(h * 0.015))
        if k % 2 == 0:
            k += 1
        smooth = np.convolve(bright_frac, np.ones(k) / k, mode="same")
        active = np.where(smooth > 0.30)[0]
        if len(active) > h * 0.30:
            best_active = active
            break
    if best_active is None:
        return box
    pad = int(h * 0.025)
    ny1 = max(y1, y1 + int(best_active[0]) - pad)
    ny2 = min(y2, y1 + int(best_active[-1]) + pad)
    if ny2 - ny1 < (y2 - y1) * 0.25:
        return box
    return (x1, ny1, x2, ny2)


def _expand_boxes_to_column_gaps(boxes, page_w, page_h):
    if len(boxes) < 2:
        return boxes
    boxes = sorted(boxes, key=lambda b: (b[0], b[1]))
    expanded = []
    for i, box in enumerate(boxes):
        x1, y1, x2, y2 = box
        if i == 0:
            nx1 = 0
        else:
            prev_x2 = boxes[i - 1][2]
            gap_left = max(0, x1 - prev_x2)
            nx1 = max(0, prev_x2 + int(gap_left * 0.08))
        if i == len(boxes) - 1:
            nx2 = page_w
        else:
            next_x1 = boxes[i + 1][0]
            gap_right = max(0, next_x1 - x2)
            nx2 = min(page_w, next_x1 - int(gap_right * 0.08))
        pad_y = int(page_h * 0.01)
        ny1 = max(0, y1 - pad_y)
        ny2 = min(page_h, y2 + pad_y)
        if nx2 - nx1 >= max(80, (x2 - x1) * 0.75):
            expanded.append((nx1, ny1, nx2, ny2))
        else:
            expanded.append(box)
    return expanded


def _boxes_are_fragmented(boxes, page_w, page_h):
    if len(boxes) <= 1:
        return True
    for i, a in enumerate(boxes):
        ax1, ay1, ax2, ay2 = a
        for b in boxes[i + 1:]:
            bx1, by1, bx2, by2 = b
            overlap_x = max(0, min(ax2, bx2) - max(ax1, bx1))
            min_w = max(1, min(ax2 - ax1, bx2 - bx1))
            same_column = overlap_x / min_w > 0.45
            vertical_gap_or_overlap = abs(ay1 - by1) > page_h * 0.15 or abs(ay2 - by2) > page_h * 0.15
            if same_column and vertical_gap_or_overlap:
                return True
    heights = [(y2 - y1) / page_h for x1, y1, x2, y2 in boxes]
    if min(heights) < 0.32 and max(heights) > 0.55:
        return True
    widths = [(x2 - x1) / page_w for x1, y1, x2, y2 in boxes]
    if len(widths) >= 3 and min(widths) < 0.18 and (max(widths) / max(min(widths), 0.001)) > 1.75:
        return True
    return False


def detect_receipts(page_img):
    rgb = page_img.copy()
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    candidate_sets = []
    parameter_sets = [
        (170, 0.045, 0.012, 0.10),
        (180, 0.050, 0.012, 0.10),
        (190, 0.065, 0.012, 0.10),
        (200, 0.090, 0.014, 0.10),
        (220, 0.075, 0.020, 0.10),
    ]
    for params in parameter_sets:
        intervals, mask = _projection_boxes(gray, dark_limit=params[0], active_threshold=params[1], smooth_frac=params[2], min_width_frac=params[3])
        if not intervals:
            continue
        boxes = []
        x_pad = int(w * 0.012)
        y_pad = int(h * 0.025)
        for x_start, x_end in intervals:
            x1 = max(0, x_start - x_pad)
            x2 = min(w, x_end + x_pad)
            crop_mask = mask[:, x1:x2]
            row_projection = crop_mask.mean(axis=1)
            row_kernel = max(31, int(h * 0.012))
            if row_kernel % 2 == 0:
                row_kernel += 1
            row_smooth = np.convolve(row_projection, np.ones(row_kernel) / row_kernel, mode="same")
            y_active = np.where(row_smooth > 0.006)[0]
            if len(y_active) == 0:
                y1, y2 = 0, h
            else:
                y1 = max(0, int(y_active[0]) - y_pad)
                y2 = min(h, int(y_active[-1]) + y_pad)
            boxes.append((x1, y1, x2, y2))
        if len(boxes) == 1 and (boxes[0][2] - boxes[0][0]) > w * 0.75:
            boxes = _split_box_by_seams(page_img, boxes[0])
        candidate_sets.append((boxes, _boxes_score(boxes, w, h), f"projection {params}"))
    paper_boxes = _paper_region_boxes(page_img)
    if paper_boxes:
        split_paper = []
        for box in paper_boxes:
            if (box[2] - box[0]) > w * 0.75:
                split_paper.extend(_split_box_by_seams(page_img, box))
            else:
                split_paper.append(box)
        candidate_sets.append((split_paper, _boxes_score(split_paper, w, h) + 100, "paper"))
    if not candidate_sets:
        return [(0, 0, w, h)]
    candidate_sets.sort(key=lambda item: item[1], reverse=True)
    boxes = sorted(candidate_sets[0][0], key=lambda b: (b[0], b[1]))
    if len(boxes) == 1 and (boxes[0][2] - boxes[0][0]) > w * 0.75:
        boxes = _split_box_by_seams(page_img, boxes[0])
    full_split = _split_box_by_seams(page_img, (0, 0, w, h))
    if len(full_split) >= 2 and (_boxes_are_fragmented(boxes, w, h) or len(boxes) == 1):
        trimmed = [_trim_box_to_bright_paper(page_img, b) for b in full_split]
        if _boxes_score(trimmed, w, h) >= _boxes_score(boxes, w, h) - 150:
            boxes = trimmed
    boxes = _expand_boxes_to_column_gaps(boxes, w, h)
    return sorted(boxes, key=lambda b: (b[0], b[1]))


def _resize_for_ocr(gray):
    h, w = gray.shape[:2]
    long_side = max(h, w)
    short_side = min(h, w)
    max_long_side = int(CONFIG.get("ocr_max_long_side", 2200) or 0)
    min_short_side = int(CONFIG.get("ocr_min_short_side", 900) or 0)
    scale = 1.0
    if max_long_side > 0 and long_side > max_long_side:
        scale = min(scale, max_long_side / float(long_side))
    if min_short_side > 0 and short_side < min_short_side:
        scale = max(scale, min_short_side / float(short_side))
    if max_long_side > 0 and long_side * scale > max_long_side * 1.15:
        scale = max_long_side / float(long_side)
    if abs(scale - 1.0) < 0.03:
        return gray
    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
    return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=interpolation)


def generate_ocr_variants(img):
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    gray = _resize_for_ocr(gray)
    return [("gray", gray)]


def _ocr_score(text):
    norm = _normalize_text(text)
    score = 0
    money_count = len(re.findall(r"\d{1,6}\s*[,.]\s*\d{1,2}", text))
    score += money_count * 8
    for token, points in [
        ("dph", 20), ("zaklad", 18), ("dan", 16), ("obrat", 16),
        ("cena celkom", 25), ("hotovost", 20), ("karta", 20),
        ("na uhradu", 25), ("doklad", 12), ("datum", 12),
        ("zaokruhlenie", 14), ("rozpis platie", 12),
    ]:
        if token in norm:
            score += points
    score += min(len(text), 2000) / 200
    return score


def _ocr_feature_summary(text):
    norm = _normalize_text(text)
    money_count = len(re.findall(r"\d{1,6}\s*[,.]\s*\d{1,2}", text))
    has_date = bool(re.search(r"\b\d{1,2}[.\/-]\s*\d{1,2}[.\/-]\s*\d{2,4}\b", text))
    has_total_keyword = any(token in norm for token in ["cena celkom", "hotovost", "hotovosť", "karta", "na uhradu", "na úhradu", "ciastka", "čiastka"])
    has_vat_keyword = any(token in norm for token in ["dph", "zaklad", "základ", "dan", "daň", "obrat", "23%", "23 %"])
    return {"score": _ocr_score(text), "money_count": money_count, "has_date": has_date, "has_total_keyword": has_total_keyword, "has_vat_keyword": has_vat_keyword}


def _fast_ocr_is_sufficient(text):
    if not text or not text.strip():
        return False
    features = _ocr_feature_summary(text)
    min_score = float(CONFIG.get("ocr_fast_accept_min_score", 95))
    min_money = int(CONFIG.get("ocr_fast_accept_min_money_count", 3))
    require_date_or_total = bool(CONFIG.get("ocr_fast_accept_require_date_or_total_keyword", True))
    if features["score"] < min_score:
        return False
    if features["money_count"] < min_money:
        return False
    if not features["has_vat_keyword"]:
        return False
    if require_date_or_total and not (features["has_date"] or features["has_total_keyword"]):
        return False
    return True


def _is_debug_line(line: str) -> bool:
    return line.strip().lower().startswith("--- ocr pokus")


def _normalize_text(value: str) -> str:
    repl = str.maketrans({
        "á": "a", "ä": "a", "č": "c", "ď": "d", "é": "e", "í": "i",
        "ĺ": "l", "ľ": "l", "ň": "n", "ó": "o", "ô": "o", "ŕ": "r",
        "š": "s", "ť": "t", "ú": "u", "ý": "y", "ž": "z",
        "Á": "a", "Ä": "a", "Č": "c", "Ď": "d", "É": "e", "Í": "i",
        "Ĺ": "l", "Ľ": "l", "Ň": "n", "Ó": "o", "Ô": "o", "Ŕ": "r",
        "Š": "s", "Ť": "t", "Ú": "u", "Ý": "y", "Ž": "z",
    })
    value = value.translate(repl).lower()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def parse_money_values(line):
    if _is_debug_line(line):
        return []
    line = line.replace("−", "-").replace("–", "-").replace("—", "-")
    values = []
    seen = set()

    def add_value(sign, euros, cents):
        if len(cents) > 2:
            cents = cents[:2]
        if len(cents) == 1:
            cents = cents + "0"
        try:
            value = float(f"{int(euros)}.{cents}")
            if sign == "-":
                value = -value
            key = round(value, 2)
            if key not in seen:
                seen.add(key)
                values.append(value)
        except ValueError:
            pass

    line_num = re.sub(r"(?<![A-Za-z0-9])b\s*([,.]\s*\d{1,3})(?![A-Za-z0-9])", r"6\1", line)
    line_num = re.sub(r"(?<![A-Za-z0-9])[OoQqDd]\s*([,.]\s*\d{1,3})(?![A-Za-z0-9])", r"0\1", line_num)
    line_num = re.sub(r"(?<![A-Za-z0-9])[Il|]\s*([,.]\s*\d{1,3})(?![A-Za-z0-9])", r"1\1", line_num)
    pattern = r"(?<!\d)([-+]?)\s*(\d{1,6})\s*[,.]\s*(\d{1,3})(?!\d)"
    for sign, euros, cents in re.findall(pattern, line_num):
        add_value(sign, euros, cents)
    ocr_digit_map = str.maketrans({"O": "0", "o": "0", "Q": "0", "q": "0", "D": "0", "d": "0", "I": "1", "l": "1", "|": "1", "S": "5", "s": "5", "b": "6", "G": "6", "g": "6", "B": "8", "A": "4"})
    fuzzy_pattern = r"(?<![A-Za-z0-9])([-+]?)\s*([0-9OoQqDdIl|SsBbGgA]{1,6})\s*[,.]\s*([0-9OoQqDdIl|SsBbGgA]{1,3})(?![A-Za-z0-9])"
    for sign, euros_raw, cents_raw in re.findall(fuzzy_pattern, line):
        euros = euros_raw.translate(ocr_digit_map)
        cents = cents_raw.translate(ocr_digit_map)
        if not euros.isdigit() or not cents.isdigit():
            continue
        add_value(sign, euros, cents)
    return values


def format_eur(value):
    if value is None:
        return None
    return round(float(value), 2)


def find_date(text):
    fuzzy_map = str.maketrans({
        "O": "0",
        "o": "0",
        "Q": "0",
        "D": "0",
        "A": "4",
        "a": "4",
        "I": "1",
        "l": "1",
        "|": "1",
        "S": "5",
        "s": "5",
        "B": "8",
        "G": "6",
        "F": "6",
    })

    def valid_date(day_i, month_i, year_i):
        return 1 <= day_i <= 31 and 1 <= month_i <= 12 and 2020 <= year_i <= 2099

    def normalize_year(year):
        year_i = int(year)
        if len(str(year)) == 2:
            year_i = 2000 + year_i
        return year_i

    def extract_dates_from_segment(segment, base_pos=0):
        found = []
        converted = segment.translate(fuzzy_map)

        # DD.MM.RRRR / DD-MM-RRRR / DD/MM/RRRR / DD MM RRRR
        for match in re.finditer(r"\b(\d{1,2})[\s.\/-]+(\d{1,2})[\s.,\/-]+(20\d{2})\b", converted):
            day, month, year = match.groups()
            try:
                day_i = int(day)
                month_i = int(month)
                year_i = normalize_year(year)
            except ValueError:
                continue

            if valid_date(day_i, month_i, year_i):
                found.append((
                    base_pos + match.start(),
                    day_i,
                    month_i,
                    year_i,
                    f"{day_i:02d}.{month_i:02d}.{year_i:04d}",
                ))

        # RRRR-MM-DD / RRRR.MM.DD / RRRR/MM/DD
        for match in re.finditer(r"\b(20\d{2})[\s.\/-]+(\d{1,2})[\s.\/-]+(\d{1,2})\b", converted):
            year, month, day = match.groups()
            try:
                day_i = int(day)
                month_i = int(month)
                year_i = int(year)
            except ValueError:
                continue

            if valid_date(day_i, month_i, year_i):
                found.append((
                    base_pos + match.start(),
                    day_i,
                    month_i,
                    year_i,
                    f"{day_i:02d}.{month_i:02d}.{year_i:04d}",
                ))

        return found

    lines = text.splitlines()

    priority_dates = []
    regular_dates = []

    offset = 0

    for idx, raw_line in enumerate(lines):
        norm_line = _normalize_text(raw_line)

        line_priority = 0

        if any(tok in norm_line for tok in [
            "datum vyhotovenia",
            "datun vyhotovenia",
            "datum vyhot",
            "vyhotovenia",
        ]):
            line_priority = 300
        elif any(tok in norm_line for tok in [
            "datum:",
            "datun:",
            "datum ",
            "datun ",
            "natum ",
            "latum ",
        ]):
            line_priority = 220
        elif any(tok in norm_line for tok in [
            "duzp",
            "datum/duzp",
        ]):
            line_priority = 120

        technical_code_line = any(tok in norm_line for tok in [
            "ekasa",
            "e-kasa",
            "uid",
            "okp",
            "pkp",
            "dkp",
            "ikp",
            "kp:",
            "kod",
            "kód",
            "ico",
            "ičo",
            "dic",
            "dič",
            "ic dph",
            "ič dph",
            "poradove cislo",
            "poradové číslo",
            "cislo dokladu",
            "číslo dokladu",
        ])

        if technical_code_line and not line_priority:
            offset += len(raw_line) + 1
            continue

        segment = raw_line

        if line_priority and idx + 1 < len(lines):
            segment = raw_line + " " + lines[idx + 1]

        line_dates = extract_dates_from_segment(segment, offset)

        for item in line_dates:
            regular_dates.append(item)

            if line_priority:
                priority_dates.append((line_priority, item[0], item[4]))

        offset += len(raw_line) + 1

    if priority_dates:
        priority_dates.sort(key=lambda item: (-item[0], item[1]))
        return priority_dates[0][2]

    compact_dates = []
    compact_text = text.translate(str.maketrans({
        "O": "0",
        "o": "0",
        "F": "6",
        "S": "5",
        "B": "8",
        "I": "1",
        "l": "1",
    }))

    for match in re.finditer(r"(?<!\d)(2\d)([01]\d)([0-3]\d)\d{3,}(?!\d)", compact_text):
        yy, mm, dd = match.groups()
        year_i = 2000 + int(yy)
        month_i = int(mm)
        day_i = int(dd)

        if valid_date(day_i, month_i, year_i):
            compact_dates.append((
                match.start(),
                day_i,
                month_i,
                year_i,
                f"{day_i:02d}.{month_i:02d}.{year_i:04d}",
            ))

    if regular_dates:
        regular_dates.sort(key=lambda item: item[0])
        return regular_dates[-1][4]

    # Kompaktné dátumy z dlhých technických čísel nepoužívame.
    # Spôsobovali falošné dátumy z eKasa / UID / OKP / DKP kódov.
    return ""

def find_date_source_debug(text):
    date_value = find_date(text)

    if not date_value:
        return ""

    try:
        day, month, year = date_value.split(".")
    except ValueError:
        return ""

    variants = [
        f"{day}.{month}.{year}",
        f"{day}-{month}-{year}",
        f"{day}/{month}/{year}",
        f"{day} {month} {year}",
        f"{int(day)}.{int(month)}.{year}",
        f"{int(day)}-{int(month)}-{year}",
        f"{year}-{month}-{day}",
        f"{year}.{month}.{day}",
        f"{year}/{month}/{day}",
    ]

    compact_variants = [
        f"{year[-2:]}{month}{day}",
        f"{year}{month}{day}",
    ]

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        converted = line.translate(str.maketrans({
            "O": "0",
            "o": "0",
            "Q": "0",
            "D": "0",
            "A": "4",
            "a": "4",
            "I": "1",
            "l": "1",
            "|": "1",
            "S": "5",
            "s": "5",
            "B": "8",
            "G": "6",
            "F": "6",
        }))

        if any(v in converted for v in variants):
            return f"bežný dátum z riadku: {line}"

        if any(v in converted for v in compact_variants):
            return f"kompaktný dátum z riadku: {line}"

    return f"zdroj dátumu sa nenašiel v riadkoch, hodnota: {date_value}"


def find_all_date_candidates_debug(text):
    """Vypíše všetky bežné dátumy nájdené v OCR texte po riadkoch."""
    found = []

    fuzzy_map = str.maketrans({
        "O": "0",
        "o": "0",
        "Q": "0",
        "D": "0",
        "A": "4",
        "a": "4",
        "I": "1",
        "l": "1",
        "|": "1",
        "S": "5",
        "s": "5",
        "B": "8",
        "G": "6",
        "F": "6",
    })

    def add_candidate(value, line_no, raw_line):
        item = f"{value} | riadok {line_no}: {raw_line.strip()}"
        if item not in found:
            found.append(item)

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue

        converted = raw_line.translate(fuzzy_map)

        # DD.MM.RRRR / DD-MM-RRRR / DD/MM/RRRR aj s medzerami
        for m in re.finditer(r"\b(\d{1,2})[\s.\/-]+(\d{1,2})[\s.,\/-]+(20\d{2})\b", converted):
            day, month, year = m.groups()

            try:
                day_i = int(day)
                month_i = int(month)
                year_i = int(year)
                # Rok musí byť 4-ciferný, napr. 2026.
            except ValueError:
                continue

            if 1 <= day_i <= 31 and 1 <= month_i <= 12 and 2020 <= year_i <= 2099:
                add_candidate(f"{day_i:02d}.{month_i:02d}.{year_i:04d}", line_no, raw_line)

        # RRRR-MM-DD / RRRR.MM.DD / RRRR/MM/DD
        for m in re.finditer(r"\b(20\d{2})[\s.\/-]+(\d{1,2})[\s.\/-]+(\d{1,2})\b", converted):
            year, month, day = m.groups()

            try:
                day_i = int(day)
                month_i = int(month)
                year_i = int(year)
            except ValueError:
                continue

            if 1 <= day_i <= 31 and 1 <= month_i <= 12 and 2020 <= year_i <= 2099:
                add_candidate(f"{day_i:02d}.{month_i:02d}.{year_i:04d}", line_no, raw_line)

    if not found:
        return "nenašiel sa žiadny bežný dátum v OCR texte"

    return " || ".join(found)


def _all_money_counter(text):
    values = []
    for line in text.splitlines():
        if _is_debug_line(line):
            continue
        values.extend(round(v, 2) for v in parse_money_values(line) if 0.01 <= abs(v) <= 100000)
    return Counter(values)


def find_payment_total(text):
    lines = [line.strip() for line in text.splitlines() if line.strip() and not _is_debug_line(line)]
    counts = _all_money_counter(text)

    change_tokens = ["vratene", "vraten", "vratit", "vydavok", "vydane", "yratene", "yraten", "yratit", "vra tene", "yra tene"]
    cash_tokens = ["hotovost", "hotovosf"]

    # Ak je na bločku HOTOVOSŤ a potom VRÁTENÉ,
    # reálna suma na úhradu je HOTOVOSŤ - VRÁTENÉ.
    cash_rows = []
    change_rows = []

    for idx, line in enumerate(lines):
        norm = _normalize_text(line)
        values = [
            round(v, 2)
            for v in parse_money_values(line)
            if 0.10 <= abs(v) <= 100000
        ]

        if not values:
            continue

        if any(tok in norm for tok in cash_tokens):
            cash_rows.append((idx, abs(values[-1]), line))

        if any(tok in norm for tok in change_tokens):
            change_rows.append((idx, abs(values[-1]), line))

    for cash_idx, cash_value, cash_line in cash_rows:
        nearby_changes = [
            (change_idx, change_value, change_line)
            for change_idx, change_value, change_line in change_rows
            if cash_idx <= change_idx <= cash_idx + 6
        ]

        if nearby_changes:
            change_idx, change_value, change_line = nearby_changes[0]
            payment = round(cash_value - change_value, 2)

            if 0.10 <= payment <= 100000:
                return payment, f"{cash_line} | {change_line} | hotovosť - vrátené"

    direct_payment_candidates = []

    for idx, line in enumerate(lines):
        norm = _normalize_text(line)

        values = [
            round(v, 2)
            for v in parse_money_values(line)
            if 0.10 <= v <= 100000
        ]

        if not values:
            continue

        priority = 0

        if any(tok in norm for tok in ["uhradene", "uhradit", "na uhradu", "uhrada"]):
            priority = 320
        elif (
            ("suma:" in norm or norm.strip().startswith("suma"))
            and not any(tok in norm for tok in ["dph", "zaklad", "zaktad", "sadzba", "obrat"])
        ):
            priority = 300
        elif "cena celkom" in norm:
            priority = 240

        if priority:
            direct_payment_candidates.append((priority, idx, values[-1], line))

    if direct_payment_candidates:
        direct_payment_candidates.sort(key=lambda item: (-item[0], item[1]))
        _priority, _idx, value, source_line = direct_payment_candidates[0]
        return value, f"{source_line} | priamy zdroj úhrady"

    keyword_weights = [
        ("uhradene eur", 240), ("uhradene", 235),
        ("uhradit eur", 235), ("uhradit", 230),
        ("na uhradu", 230), ("na ohradu", 220), ("nauhradu", 220), ("naohradu", 215),
        ("uhradu eur", 220), ("ohradu eur", 210),
        ("uhrada eur", 215), ("uhrada", 210),
        ("ciastka eur", 180), ("ciastka:", 180), ("ciastka", 170), ("clastka", 160), ("ciastka, eur", 170),
        ("na uhradu", 155), ("na ohradu", 150), ("nauhradu", 150), ("naohradu", 150),
        ("uhradu eur", 150), ("ohradu eur", 145),
        ("cena celkom", 145), ("cena celkom:", 150), ("lena cel", 115),
        ("celkom:", 135), ("celkom", 125), ("clkom", 95), ("cekkom", 95), ("ceikom", 95),
        ("sucet", 130), ("su cet", 120),
        ("medzisucet", 110), ("medztsucet", 105), ("nedzisuce", 105),
        ("karta", 100), ("kartou", 100),
        ("hotovost", 82), ("hotovost:", 82), ("hotovosf", 75),
        ("rozpis platie", 45),
        ("suma:", 220), ("suma", 190), ("spolu", 35),
    ]
    candidates = []
    document_has_change = any(any(tok in _normalize_text(line) for tok in change_tokens) for line in lines)

    def add_candidates_from_line(idx, line, base_weight, source_line):
        values = [round(v, 2) for v in parse_money_values(line) if 0.10 <= abs(v) <= 100000]
        norm_source = _normalize_text(source_line)

        is_item_line = (
            "eur/l" in norm_source
            or "eur / l" in norm_source
            or any(tok in norm_source for tok in [
                "nafta",
                "diesel",
                "benzin",
                "benzín",
                "natural",
                "evo ",
            ])
            or re.search(r"\b\d+(?:[,.]\d+)?\s*l\b", norm_source) is not None
        )

        is_real_payment_line = any(tok in norm_source for tok in [
            "uhradene",
            "uhradit",
            "na uhradu",
            "uhrada",
            "cena celkom",
            "ciastka",
            "clastka",
            "suma",
            "hotovost:",
            "hotovosf:",
            "karta:",
            "kartou:",
        ])

        is_vat_table_source = (
            any(tok in norm_source for tok in [
                "zaklad",
                "zaktad",
                "sadzba",
                "sadzby",
                "dph",
                "oph",
                "obrat",
                "rekapitulacia",
                "triedy",
            ])
            and not is_real_payment_line
        )

        for value in values:
            if value <= 0:
                continue

            if (is_item_line or is_vat_table_source) and not is_real_payment_line:
                continue

            repeat_bonus = min(counts[value], 4) * 18
            last_bonus = 10 if values and value == values[-1] else 0
            score = base_weight + repeat_bonus + last_bonus
            value_count = len(values)
            if document_has_change and any(tok in norm_source for tok in ["hotovost", "hotovosf"]):
                score -= 95
            if any(tok in norm_source for tok in ["vratene", "vraten", "vratit", "vydavok", "vydane"]):
                score -= 180
            if any(tok in norm_source for tok in ["celkom", "clkom", "cekkom", "ceikom"]) and "cena celkom" not in norm_source:
                if value_count == 1:
                    score += 70
                elif value_count >= 2:
                    score -= 90
            if any(tok in norm_source for tok in ["rekapitulacia", "sadzba", "zaklad", "zaktad", "dph", "oph", "bez dph", "bezdp h", "a 23", "23x", "23%"]) and not any(tok in norm_source for tok in ["ciastka", "clastka", "cena celkom", "karta", "na uhradu", "uhradene", "uhradit", "uhrada", "hotovost", "sucet", "medzisucet"]):
                score -= 140
            if re.search(r"\d{1,6}\s*[,.]\s*\d(?!\d)", source_line) and not re.search(r"\d{1,6}\s*[,.]\s*\d{2}(?!\d)", source_line):
                score -= 35
            candidates.append((score, value, source_line, idx))

    for idx, line in enumerate(lines):
        norm = _normalize_text(line)
        weight = 0
        for keyword, keyword_weight in keyword_weights:
            if keyword in norm:
                weight = max(weight, keyword_weight)
        if weight == 0:
            continue
        add_candidates_from_line(idx, line, weight, line)

        method_only_payment_line = (
            "platba" in norm
            and any(tok in norm for tok in ["hotovost", "hotovosf", "karta", "kartou"])
            and not parse_money_values(line)
        )

        if not parse_money_values(line) and not method_only_payment_line:
            for j in range(idx + 1, min(len(lines), idx + 3)):
                add_candidates_from_line(j, lines[j], max(30, weight - 12), f"{line} | {lines[j]}")

    if candidates:
        candidates.sort(reverse=True, key=lambda item: item[0])
        return candidates[0][1], candidates[0][2]

    fallback = []
    for idx, line in enumerate(lines):
        norm = _normalize_text(line)
        if "eur" not in norm and "fur" not in norm and "evr" not in norm:
            continue
        if any(tok in norm for tok in ["rekapitulacia", "sadzba", "zaklad", "zaktad", "dph", "oph", "bez dph"]):
            continue
        for value in [round(v, 2) for v in parse_money_values(line) if 0.10 <= v <= 100000]:
            score = min(counts[value], 4) * 20
            if value < 1000:
                score += 12
            if value < 100:
                score += 8
            fallback.append((score, value, line, idx))
    if fallback:
        fallback.sort(reverse=True, key=lambda item: item[0])
        if fallback[0][0] >= 20:
            return fallback[0][1], fallback[0][2]
    return None, ""


def find_total(text):
    total, _source = find_payment_total(text)
    return total


def find_rounding_amount(text):
    lines = [line.strip() for line in text.splitlines() if line.strip() and not _is_debug_line(line)]
    candidates = []

    def signed_values_from_rounding_line(line):
        vals = [round(v, 2) for v in parse_money_values(line) if abs(v) <= 5]
        if not vals:
            return None
        norm = line.replace("−", "-").replace("–", "-").replace("—", "-")
        has_negative_hint = bool(re.search(r"[-]\s*0\s*[,.]\s*\d{1,2}", norm))
        if not has_negative_hint and re.search(r"[\"'`´""|«‹]\s*0\s*[,.]\s*\d{1,2}", norm):
            has_negative_hint = True
        if has_negative_hint and vals[-1] > 0:
            vals[-1] = -vals[-1]
        return vals, has_negative_hint

    for idx, line in enumerate(lines):
        norm = _normalize_text(line)
        if not any(token in norm for token in ["zaokruhlen", "zaokruhl", "zaokr", "rnd", "rno", "knu"]):
            continue
        result = signed_values_from_rounding_line(line)
        if result:
            values, neg_hint = result
            value = values[-1]
            if abs(value) <= 0.05:
                candidates.append((1 if neg_hint else 0, idx, value, line))
            continue
        if idx + 1 < len(lines):
            nxt = lines[idx + 1]
            result = signed_values_from_rounding_line(nxt)
            if result:
                values, neg_hint = result
                value = values[-1]
                if abs(value) <= 0.05:
                    candidates.append((1 if neg_hint else 0, idx, value, f"{line} | {nxt}"))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]))
        _neg, _idx, value, source = candidates[-1]
        return value, source
    return 0.0, ""



def find_gross_total_before_rounding(text):
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not _is_debug_line(line)
    ]

    candidates = []

    for idx, line in enumerate(lines):
        norm = _normalize_text(line)

        values = [
            round(v, 2)
            for v in parse_money_values(line)
            if 0.10 <= v <= 100000
        ]

        if not values:
            continue

        score = 0

        if "pred zaokruhlenim" in norm or "pred zaokr" in norm:
            score = 360
        elif "cena celkom" in norm:
            score = 310
        elif "rekapitulacia obratu" in norm:
            score = 300
        elif "obrat" in norm:
            score = 290
        elif norm.startswith("spolu") or " spolu:" in norm:
            score = 260
        elif "sadzby" in norm or "sadzba" in norm or "triedy" in norm:
            score = 220

        if score == 0:
            continue

        # Riadky platby nie sú Spolu s DPH pred zaokrúhlením.
        if any(tok in norm for tok in [
            "uhradene",
            "uhradit",
            "na uhradu",
            "hotovost",
            "vratene",
            "vydavok",
        ]) and "pred zaokruhlenim" not in norm:
            continue

        # Berieme najväčšiu kladnú sumu z riadku.
        value = max(values)

        candidates.append((score, idx, value, line))

    if candidates:
        candidates.sort(key=lambda item: (-item[0], item[1]))
        _score, _idx, value, source_line = candidates[0]
        return value, source_line

    return None, ""

def _extract_vat_line_candidates(line, total=None, rounding=0.0, prev_norm=""):
    if _is_debug_line(line):
        return []
    norm = _normalize_text(line)
    values = [round(v, 2) for v in parse_money_values(line) if 0.00 <= abs(v) <= 100000]
    if not values:
        return []

    item_context = f"{prev_norm} {norm}"

    is_item_line = (
        "eur/l" in item_context
        or "eur / l" in item_context
        or any(tok in item_context for tok in [
            "nafta",
            "diesel",
            "benzin",
            "benzín",
            "natural",
            "evo ",
        ])
        or re.search(r"\b\d+(?:[,.]\d+)?\s*l\b", item_context) is not None
    )

    vat_table_hint = any(token in norm or token in prev_norm for token in [
        "sadzba",
        "zaklad",
        "zaktad",
        "dph",
        "oph",
        "dan",
        "nan",
        "obrat",
        "rekapitulacia",
        "triedy",
        "spolu:",
    ])

    if is_item_line and not vat_table_hint:
        return []

    vat_context = any(token in norm for token in ["sadzba", "zaklad", "zaktad", "dph", "oph", "dan", "nan", "obrat", "23%", "23 %", "03%", "03 %", "2s%", "2s %", "a 23", "a23"])
    if any(token in prev_norm for token in ["sadzba", "zaklad", "zaktad", "dph", "oph", "dan", "nan", "obrat", "rekapitulacia obratu", "rekapitulacia"]):
        vat_context = True
    if "celkom" in norm and ("23" in prev_norm or "%" in prev_norm):
        vat_context = True
    if re.search(r"\b(?:23|2s|2z|03|3)\s*%", line, re.IGNORECASE):
        vat_context = True
    if not vat_context:
        return []
    candidates = []

    def add_candidate(zaklad, dph, obrat, source_note):
        if zaklad is None or dph is None:
            return
        zaklad = round(float(zaklad), 2)
        dph = round(float(dph), 2)
        obrat = round(float(obrat if obrat is not None else zaklad + dph), 2)
        if zaklad <= 0 or dph <= 0 or obrat <= 0:
            return
        source_note_extra = ""
        sum_diff = abs((zaklad + dph) - obrat)
        if sum_diff > 0.08:
            corrected_zaklad = round(obrat - dph, 2)
            corrected_ratio = dph / corrected_zaklad if corrected_zaklad else 0
            expected_total = round(obrat + (rounding or 0.0), 2)
            total_supports_obrat = total is not None and abs(float(total) - expected_total) <= max(0.10, abs(expected_total) * 0.01)
            strong_vat_context = vat_context and ("23" in norm or "23" in prev_norm or "%" in norm or "%" in prev_norm or "dph" in norm or "dph" in prev_norm or "dan" in norm or "dan" in prev_norm or "zaklad" in norm or "zaklad" in prev_norm)
            ratio_supports_obrat = abs(corrected_ratio - 0.23) <= 0.045
            if corrected_zaklad > 0 and corrected_zaklad >= dph and 0.15 <= corrected_ratio <= 0.30 and (total_supports_obrat or (strong_vat_context and ratio_supports_obrat)):
                zaklad = corrected_zaklad
                source_note_extra = " | základ opravený"
            else:
                return
        if zaklad < dph:
            return
        ratio = dph / zaklad if zaklad else 0
        if not (0.15 <= ratio <= 0.30):
            return
        score = 100
        if "obrat" in source_note:
            score += 55
        elif "základ a DPH" in source_note and len(values) >= 3:
            score -= 55
        if "23" in norm or "23" in prev_norm:
            score += 25
        if "dph" in norm or "oph" in norm or "dan" in norm or "nan" in norm or "dph" in prev_norm or "dan" in prev_norm:
            score += 18
        if "zaklad" in norm or "zaktad" in norm or "zaklad" in prev_norm:
            score += 12
        if "obrat" in norm or "obrat" in prev_norm:
            score += 12
        if "rekapitulacia" in prev_norm or "rekapitulacia" in norm:
            score += 5
        score += max(0, 30 - abs(ratio - 0.23) * 550)
        expected_total = round(obrat + (rounding or 0.0), 2)
        if total is not None:
            pay_diff = abs(expected_total - total)
            if pay_diff <= 0.05:
                score += 90
            elif pay_diff <= 0.20:
                score += 30
            elif pay_diff > max(0.50, total * 0.05):
                score -= 180
        candidates.append({"score": score, "zaklad_dph": zaklad, "dph": dph, "obrat_dph": obrat, "spolu_s_dph": obrat, "ratio": ratio, "source_line": line, "source_note": f"{source_note}{source_note_extra}"})

    if len(values) == 2 and vat_table_hint:
        possible_dph = values[0]
        possible_obrat = values[1]
        possible_zaklad = round(possible_obrat - possible_dph, 2)

        if possible_zaklad > 0:
            add_candidate(
                possible_zaklad,
                possible_dph,
                possible_obrat,
                "DPH tabuľka bez základu - základ dopočítaný"
            )

    if len(values) >= 3 and vat_table_hint and values[0] < 1:
        possible_dph = values[-2]
        possible_obrat = values[-1]
        possible_zaklad = round(possible_obrat - possible_dph, 2)

        if possible_zaklad > 0:
            add_candidate(
                possible_zaklad,
                possible_dph,
                possible_obrat,
                "DPH tabuľka s poškodeným základom - základ dopočítaný"
            )

    if len(values) == 2 and vat_table_hint:
        # METRO a podobné bločky: OCR niekedy poškodí stĺpec DPH,
        # ale správne prečíta základ a spolu, napr. "28,47 ... 35,02".
        possible_zaklad = values[0]
        possible_obrat = values[1]
        possible_dph = round(possible_obrat - possible_zaklad, 2)

        if possible_zaklad > 0 and possible_obrat > possible_zaklad:
            ratio = possible_dph / possible_zaklad
            if 0.18 <= ratio <= 0.30:
                add_candidate(
                    possible_zaklad,
                    possible_dph,
                    possible_obrat,
                    "DPH tabuľka - DPH dopočítaná zo základu a spolu"
                )

    if len(values) >= 3 and vat_table_hint:
        # Ak OCR riadok obsahuje základ a celkovú sumu, ale DPH stĺpec je poškodený
        # napr. "23h 28,47 0,595 35,02", dopočítame DPH = spolu - základ.
        for i, possible_zaklad in enumerate(values):
            for j, possible_obrat in enumerate(values):
                if i == j:
                    continue
                if possible_zaklad <= 0 or possible_obrat <= possible_zaklad:
                    continue

                possible_dph = round(possible_obrat - possible_zaklad, 2)
                ratio = possible_dph / possible_zaklad

                if not (0.18 <= ratio <= 0.30):
                    continue

                add_candidate(
                    possible_zaklad,
                    possible_dph,
                    possible_obrat,
                    "DPH tabuľka - DPH dopočítaná zo základu a spolu"
                )

    if len(values) >= 3:
        for i in range(0, len(values) - 2):
            add_candidate(values[i], values[i + 1], values[i + 2], "riadok obsahuje základ, DPH/daň a obrat")
        add_candidate(values[-3], values[-2], values[-1], "riadok obsahuje základ, DPH/daň a obrat")

    has_valid_triple_candidate = len(values) >= 3 and bool(candidates)

    strict_vat_triple_context = (
        len(values) >= 3
        and any(tok in norm or tok in prev_norm for tok in [
            "sadzba",
            "sadzby",
            "zaklad",
            "zaktad",
            "dph",
            "oph",
            "dan",
            "nan",
            "obrat",
            "spolu",
            "triedy",
            "rekapitulacia",
        ])
    )

    if has_valid_triple_candidate or strict_vat_triple_context:
        return candidates

    for i, zaklad in enumerate(values):
        for j, dph in enumerate(values):
            if i == j:
                continue
            if zaklad <= 0 or dph <= 0:
                continue
            if zaklad < dph:
                continue
            ratio = dph / zaklad
            if not (0.18 <= ratio <= 0.30):
                continue
            add_candidate(zaklad, dph, None, "základ a DPH z riadku")

    return candidates


def find_vat_table(text, total=None):
    lines = [line.strip() for line in text.splitlines() if line.strip() and not _is_debug_line(line)]
    payment_total, payment_source = find_payment_total(text)
    if payment_total is not None:
        total = payment_total
    rounding, rounding_source = find_rounding_amount(text)

    gross_total, gross_source = find_gross_total_before_rounding(text)

    if gross_total is not None and payment_total is not None:
        calculated_rounding = round(float(payment_total) - float(gross_total), 2)
        if abs(calculated_rounding) <= 0.10:
            rounding = calculated_rounding
            if not rounding_source:
                rounding_source = "zaokrúhlenie dopočítané z úhrady a sumy pred zaokrúhlením"

    best = {"zaklad_dph": None, "dph": None, "obrat_dph": None, "zaokruhlenie": rounding, "spolu_s_dph": total, "sadzba_dph": "", "payment_total": total, "payment_source": payment_source, "rounding_source": rounding_source, "vat_source": ""}
    candidate_rows = []
    for idx, line in enumerate(lines):
        prev_norm = _normalize_text(lines[idx - 1]) if idx > 0 else ""
        for candidate in _extract_vat_line_candidates(line, total=total, rounding=rounding, prev_norm=prev_norm):
            candidate_rows.append(candidate)
    if candidate_rows:
        candidate_rows.sort(reverse=True, key=lambda item: item["score"])
        selected = candidate_rows[0]
        obrat_dph = round(float(selected["obrat_dph"]), 2)

        # DPH tabuľka má prednosť pre Obrat/Spolu.
        # Ak máme reálnu úhradu, zaokrúhlenie dopočítame z úhrady a obratu.
        if payment_total is not None:
            computed_rounding = round(float(payment_total) - obrat_dph, 2)

            if rounding is None or abs(float(rounding) - computed_rounding) > 0.02:
                rounding = computed_rounding
                rounding_source = "zaokrúhlenie opravené podľa úhrady a DPH tabuľky"

        expected_payment_total = round(obrat_dph + float(rounding or 0.0), 2)

        payment_source_norm = _normalize_text(payment_source or "")

        direct_payment_source = any(tok in payment_source_norm for tok in [
            "uhradene",
            "uhradit",
            "na uhradu",
            "uhrada",
            "hotovost",
            "hotovosf",
            "karta",
            "kartou",
            "ciastka",
            "clastka",
        ])

        gross_total_source = any(tok in payment_source_norm for tok in [
            "celkom",
            "clkom",
            "cekkom",
            "ceikom",
            "spolu",
            "sucet",
            "medzisucet",
            "cena celkom",
            "obrat",
        ])

        if total is None:
            total = expected_payment_total
            payment_source = payment_source or "úhrada odvodená z Obrat DPH + zaokrúhlenie"
        else:
            total = round(float(total), 2)

            if abs(float(rounding or 0.0)) > 0.0001:
                total_equals_obrat = abs(total - obrat_dph) <= 0.10
                total_differs_from_expected_payment = abs(total - expected_payment_total) > 0.05

                # Ak sa ako Suma na úhradu omylom zobralo CELKOM/SPOLU,
                # ponecháme to ako Spolu s DPH a Suma na úhradu sa dopočíta cez zaokrúhlenie.
                if gross_total_source and not direct_payment_source and total_equals_obrat:
                    total = expected_payment_total
                    payment_source = f"{payment_source} | úhrada opravená podľa Obrat DPH + zaokrúhlenie"

                elif not direct_payment_source and total_differs_from_expected_payment:
                    total = expected_payment_total
                    payment_source = payment_source or "úhrada odvodená z Obrat DPH + zaokrúhlenie"

        calculated_rounding = round(float(total) - obrat_dph, 2) if total is not None else rounding

        if calculated_rounding is not None and abs(calculated_rounding) <= 0.10:
            rounding = calculated_rounding

        best["zaklad_dph"] = selected["zaklad_dph"]
        best["dph"] = selected["dph"]
        best["obrat_dph"] = obrat_dph
        best["spolu_s_dph"] = obrat_dph
        best["payment_total"] = round(total, 2) if total is not None else None
        best["zaokruhlenie"] = rounding
        best["payment_source"] = payment_source
        best["sadzba_dph"] = "23 %"
        best["vat_source"] = f"DPH z bloku: {selected['source_line']}"
        return best
    if gross_total is not None:
        gross_total = round(float(gross_total), 2)

        if total is None:
            total = round(gross_total + float(rounding or 0.0), 2)

        payment_total = round(float(total), 2)

        text_norm = _normalize_text(text)

        if "23" in text_norm:
            zaklad_dph = round(gross_total / 1.23, 2)
            dph = round(gross_total - zaklad_dph, 2)

            best["zaklad_dph"] = zaklad_dph
            best["dph"] = dph
            best["obrat_dph"] = gross_total
            best["spolu_s_dph"] = gross_total
            best["payment_total"] = payment_total
            best["zaokruhlenie"] = round(payment_total - gross_total, 2)
            best["sadzba_dph"] = "23 %"
            best["vat_source"] = f"DPH dopočítaná z 23 % a Spolu s DPH: {gross_source}"
            return best

        best["spolu_s_dph"] = gross_total
        best["payment_total"] = payment_total
        best["zaokruhlenie"] = round(payment_total - gross_total, 2)
        best["vat_source"] = f"DPH sa nepodarilo prečítať, Spolu s DPH: {gross_source}"
        return best

    if total is not None:
        payment_total = round(float(total), 2)
        gross_total = round(payment_total - float(rounding or 0.0), 2)

        best["spolu_s_dph"] = gross_total
        best["payment_total"] = payment_total
        best["zaokruhlenie"] = round(payment_total - gross_total, 2)
        best["vat_source"] = "DPH sa nepodarilo prečítať"

    return best


def find_company_name(text):
    """Extrahuje názov firmy/spoločnosti z prvých riadkov bločku.
    Hľadá riadky obsahujúce 's.r.o', 'a.s.', 'k.s.', 'spol.' atď.
    Ak nenájde, vráti prvé zmysluplné riadky hlavičky.
    """
    # Vzory pre právne formy
    legal_re = re.compile(
        r"\b(s\.?\s*r\.?\s*o\.?|a\.?\s*s\.?|k\.?\s*s\.?|v\.?\s*o\.?\s*s\.?|"
        r"spol\.|s\.?\s*p\.?|z\.?\s*o\.?\s*o\.?|ltd\.?|gmbh|inc\.?|"
        r"o\.?\s*z\.?|n\.?\s*o\.?|s\.?\s*r\.?\s*o|a\.?\s*s)\b",
        re.IGNORECASE,
    )
    # Tokeny ktoré naznačujú, že ide o administrative riadok (nie hlavičku firmy)
    skip_tokens = [
        "dph", "zaklad", "dan", "obrat", "spolu", "celkom", "uhrad",
        "hotovost", "karta", "platba", "medzisucet", "pokladnica", "datum",
        "datun", "ico", "dic", "cislo", "registr", "uid", "qr", "ekasa",
        "autoriz", "potvrdenka", "dakujem", "uschovajte", "bodov",
        "overenie", "terminal", "mastercard", "visa", "contactless",
        "zaokruh", "sadzba",
    ]

    def is_skip(norm):
        return any(t in norm for t in skip_tokens)

    def clean(line):
        line = re.sub(r"\s+", " ", line).strip(" |;:-.,")
        return line

    raw_lines = [l.strip() for l in text.splitlines()]
    # Berieme len prvých 20 riadkov (hlavička dokladu)
    top_lines = []
    for l in raw_lines[:40]:
        if _is_debug_line(l):
            continue
        if not l:
            continue
        top_lines.append(l)
        if len(top_lines) >= 20:
            break

    # Priorita 1: riadok s právnou formou
    for line in top_lines:
        norm = _normalize_text(line)
        if is_skip(norm):
            continue
        if legal_re.search(line):
            letters = re.sub(r"[^a-zA-ZÀ-žÀ-ž]", "", line)
            if len(letters) >= 3:
                return clean(line)

    # Priorita 2: prvé 2 zmysluplné riadky bez čísel, bez skip tokenov
    header_parts = []
    for line in top_lines:
        norm = _normalize_text(line)
        if is_skip(norm):
            continue
        # Preskočiť riadky kde väčšina znakov sú číslice alebo špeciálne znaky
        letters = re.sub(r"[^a-zA-ZÀ-žÀ-ž\s]", "", line).strip()
        if len(letters) < 3:
            continue
        # Preskočiť príliš krátke riadky (napr. "OK", "1")
        if len(line.strip()) < 3:
            continue
        header_parts.append(clean(line))
        if len(header_parts) >= 2:
            break

    return " | ".join(header_parts)


def parse_receipt_text(text, receipt_id, file_name):
    payment_total, payment_source = find_payment_total(text)
    vat = find_vat_table(text, total=payment_total)
    return {
        "nazovSuboru": file_name,
        "doklad": receipt_id if receipt_id != 0 else None,
        "stav": "OK",
        "datumVystavenia": find_date(text),
        "dateSource": find_date_source_debug(text),
        "dateCandidates": find_all_date_candidates_debug(text),
        "sadzbaDph": vat["sadzba_dph"],
        "zakladDph": format_eur(vat["zaklad_dph"]),
        "dph": format_eur(vat["dph"]),
        "obratDph": format_eur(vat.get("obrat_dph")),
        "zaokruhlenie": format_eur(vat.get("zaokruhlenie", 0.0)),
        "spoluSDph": format_eur(vat["spolu_s_dph"]),
        "sumaNaUhradu": format_eur(vat.get("payment_total", payment_total)),
        "popisNajvacsejPolozky": find_company_name(text),
        "paymentSource": vat.get("payment_source", payment_source),
        "vatSource": vat.get("vat_source", ""),
        "roundingSource": vat.get("rounding_source", ""),
    }


def is_valid_receipt_row(row):
    has_date = bool(row.get("datumVystavenia"))
    has_total = row.get("sumaNaUhradu") is not None or row.get("spoluSDph") is not None
    has_vat = row.get("dph") is not None
    has_item = bool(row.get("popisNajvacsejPolozky"))
    return (has_total or has_vat) and (has_date or has_item)


def make_not_found_row(file_name):
    return {
        "nazovSuboru": file_name,
        "doklad": None,
        "stav": "blok nenajdeny",
        "datumVystavenia": "",
        "dateSource": "",
        "dateCandidates": "",
        "sadzbaDph": "",
        "zakladDph": None,
        "dph": None,
        "obratDph": None,
        "zaokruhlenie": None,
        "spoluSDph": None,
        "sumaNaUhradu": None,
        "popisNajvacsejPolozky": "",
        "paymentSource": "",
        "vatSource": "",
        "roundingSource": "",
        "ocrTextPreview": "",
    }


def run_ocr(img):
    ocr_results = []
    calls = 0

    def combine_results(limit=8):
        if not ocr_results:
            return ""
        ocr_results.sort(reverse=True, key=lambda item: item[0])
        combined_parts = []
        seen_lines = set()
        for score, label, text in ocr_results[:limit]:
            combined_parts.append(f"\n--- OCR pokus {label}, score={score:.1f} ---")
            for line in text.splitlines():
                clean = line.strip()
                if not clean:
                    continue
                key = re.sub(r"\s+", " ", clean.lower())
                if key in seen_lines:
                    continue
                seen_lines.add(key)
                combined_parts.append(clean)
        return "\n".join(combined_parts).strip()

    def _parsed_text_has_key_fields(text):
        if not text or not text.strip():
            return False
        try:
            row = parse_receipt_text(text, 0, "__ocr_check__")
        except Exception:
            return False
        total = row.get("sumaNaUhradu") is not None or row.get("spoluSDph") is not None
        dph = row.get("dph") is not None
        zaklad = row.get("zakladDph") is not None
        obrat = row.get("obratDph") is not None
        date = bool(row.get("datumVystavenia"))
        # OCR nezastavujeme iba podľa sumy a DPH.
        # Pri Blok_3 sa stalo, že skorší OCR variant našiel sumy,
        # ale dátum bol zlý alebo chýbal. Lepší variant PSM 6 mal dátum správne.
        if date and total and (dph or obrat or zaklad):
            return True

        return False

    def should_stop_progressively():
        if not CONFIG.get("ocr_progressive_stop", True):
            return False
        return _parsed_text_has_key_fields(combine_results(limit=8))

    def run_one(part_img, label, psm):
        nonlocal calls
        variants = dict(generate_ocr_variants(part_img))
        processed = variants.get("gray")
        if processed is None:
            return False
        timeout = int(CONFIG.get("tesseract_timeout_seconds", 30))
        primary_lang = str(CONFIG.get("ocr_language", "slk+eng") or "slk+eng")
        fallback_lang = str(CONFIG.get("ocr_fallback_language", "eng") or "").strip()
        config_str = f"--oem 3 --psm {psm}"
        calls += 1
        try:
            text = pytesseract.image_to_string(processed, lang=primary_lang, config=config_str, timeout=timeout)
        except RuntimeError:
            text = ""
        except Exception:
            text = ""
        if not text.strip() and fallback_lang:
            try:
                text = pytesseract.image_to_string(processed, lang=fallback_lang, config=config_str, timeout=timeout)
            except Exception:
                text = ""
        if text.strip():
            ocr_results.append((_ocr_score(text), f"{label}:gray:psm{psm}", text))
        return should_stop_progressively()

    h, w = img.shape[:2]
    gray0 = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    faint_mode = str(CONFIG.get("faint_scan_mode", "auto")).strip().lower()
    if faint_mode == "always":
        faint = True
    elif faint_mode in {"off", "false", "0", "disabled"}:
        faint = False
    else:
        faint = (float(np.percentile(gray0, 95)) < 240) or (float(np.median(gray0)) < 230)

    if CONFIG.get("ocr_fast_first", True):
        stopped = run_one(img, "fast_full", 6)
        fast_text = combine_results(limit=3)
        fast_ok = _fast_ocr_is_sufficient(fast_text)
        parsed_ok = stopped or _parsed_text_has_key_fields(fast_text)
        if fast_text and faint_mode != "always" and parsed_ok:
            return fast_text

    max_calls = int(CONFIG.get("ocr_faint_max_calls", 5) or 0)

    def call_allowed():
        return max_calls <= 0 or calls < max_calls

    if faint or faint_mode == "always":
        stages = [(img, "full", 4)]
        payband = img[int(h * 0.36):int(h * 0.84), :]
        if payband.size:
            stages.append((payband, "payband", 6))
        dphtable = img[int(h * 0.43):int(h * 0.75), :]
        if dphtable.size:
            stages.append((dphtable, "dphtable", 6))
        topband = img[0:int(h * 0.48), :]
        if topband.size:
            stages.append((topband, "topband", 6))
        if dphtable.size:
            stages.append((dphtable, "dphtable", 4))
        if topband.size:
            stages.append((topband, "topband", 4))
        for part_img, label, psm in stages:
            if not call_allowed():
                break
            if run_one(part_img, label, psm):
                break
    else:
        stages = [(img, "full", 4)]
        lowerband = img[int(h * 0.35):, :]
        if lowerband.size:
            stages.append((lowerband, "lowerband", 6))
        for part_img, label, psm in stages:
            if run_one(part_img, label, psm):
                break

    return combine_results(limit=8)


def save_excel(rows, output_path: Path):
    excel_rows = []
    for row in rows:
        excel_rows.append({
            "Názov súboru": row.get("nazovSuboru", ""),
            "Doklad": row.get("doklad", ""),
            "Stav": row.get("stav", ""),
            "Dátum vystavenia": row.get("datumVystavenia", ""),
            "Základ DPH": row.get("zakladDph"),
            "DPH": row.get("dph"),
            "Spolu s DPH": row.get("spoluSDph"),
            "Text": row.get("popisNajvacsejPolozky", ""),
            "Zaokrúhlenie": row.get("zaokruhlenie"),
            "Suma na úhradu": row.get("sumaNaUhradu"),
            "Sadzba DPH": row.get("sadzbaDph", ""),
            "Obrat DPH": row.get("obratDph"),
            "Zdroj úhrady": row.get("paymentSource", ""),
            "Zdroj DPH": row.get("vatSource", ""),
            "Zdroj zaokrúhlenia": row.get("roundingSource", ""),
            "Zdroj dátumu": row.get("dateSource", ""),
            "Všetky dátumy OCR": row.get("dateCandidates", ""),
            "OCR text ukážka": row.get("ocrTextPreview", ""),
        })
    df = pd.DataFrame(excel_rows)
    visible_cols = [
        "Názov súboru",
        "Doklad",
        "Stav",
        "Dátum vystavenia",
        "Základ DPH",
        "DPH",
        "Spolu s DPH",
        "Text",
        "Zaokrúhlenie",
        "Suma na úhradu",
        "Sadzba DPH",
        "Obrat DPH",
    ]
    for col in visible_cols:
        if col not in df.columns:
            df[col] = ""
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df[visible_cols].to_excel(writer, sheet_name="Doklady", index=False)
        ws = writer.book["Doklady"]
        ws["M1"] = "Check DPH"
        ws["N1"] = "Check úhrady"
        ws["O1"] = "Kontrola"
        ws["P1"] = "Zdroj úhrady"
        ws["Q1"] = "Zdroj DPH"
        ws["R1"] = "Zdroj zaokrúhlenia"
        ws["S1"] = "Zdroj dátumu"
        ws["T1"] = "Všetky dátumy OCR"
        ws["U1"] = "OCR text ukážka"
        ws["V1"] = "Check sadzby DPH"

        tolerance = 0.02

        def parse_excel_money(value):
            if value is None:
                return None

            if isinstance(value, (int, float)):
                return float(value)

            txt = str(value).strip()
            txt = txt.replace("€", "")
            txt = txt.replace("\u00a0", "")
            txt = txt.replace(" ", "")

            if not txt:
                return None

            if "," in txt:
                txt = txt.replace(".", "")
                txt = txt.replace(",", ".")

            try:
                return float(txt)
            except Exception:
                return None

        for row_idx in range(2, ws.max_row + 1):
            zaklad = parse_excel_money(ws[f"E{row_idx}"].value)
            dph = parse_excel_money(ws[f"F{row_idx}"].value)
            spolu = parse_excel_money(ws[f"G{row_idx}"].value)
            zaokruhlenie = parse_excel_money(ws[f"H{row_idx}"].value)
            suma_na_uhradu = parse_excel_money(ws[f"I{row_idx}"].value)
            obrat = parse_excel_money(ws[f"L{row_idx}"].value)

            check_dph = None
            if zaklad is not None and dph is not None and obrat is not None:
                check_dph = round(zaklad + dph - obrat, 2)

            check_uhrady = None
            if suma_na_uhradu is not None and spolu is not None and zaokruhlenie is not None:
                check_uhrady = round(suma_na_uhradu - spolu - zaokruhlenie, 2)

            ws[f"M{row_idx}"] = check_dph
            ws[f"N{row_idx}"] = check_uhrady

            if check_dph is None and check_uhrady is None:
                ws[f"O{row_idx}"] = ""
            elif (
                (check_dph is not None and abs(check_dph) > tolerance)
                or
                (check_uhrady is not None and abs(check_uhrady) > tolerance)
            ):
                ws[f"O{row_idx}"] = "Chyba"
            else:
                ws[f"O{row_idx}"] = "OK"

            data_idx = row_idx - 2
            if 0 <= data_idx < len(df):
                ws[f"P{row_idx}"] = df.iloc[data_idx].get("Zdroj úhrady", "")
                ws[f"Q{row_idx}"] = df.iloc[data_idx].get("Zdroj DPH", "")
                ws[f"R{row_idx}"] = df.iloc[data_idx].get("Zdroj zaokrúhlenia", "")
                ws[f"S{row_idx}"] = df.iloc[data_idx].get("Zdroj dátumu", "")
                ws[f"T{row_idx}"] = df.iloc[data_idx].get("Všetky dátumy OCR", "")
                ws[f"U{row_idx}"] = df.iloc[data_idx].get("OCR text ukážka", "")

                zaklad_rate = parse_excel_money(ws[f"E{row_idx}"].value)
                dph_rate = parse_excel_money(ws[f"F{row_idx}"].value)
                sadzba_raw = str(ws[f"K{row_idx}"].value or "")

                sadzba_match = re.search(r"(\d+(?:[,.]\d+)?)", sadzba_raw)

                if zaklad_rate is not None and dph_rate is not None and sadzba_match:
                    sadzba_rate = float(sadzba_match.group(1).replace(",", "."))
                    expected_dph = round(zaklad_rate * sadzba_rate / 100.0, 2)
                    check_sadzby = round(dph_rate - expected_dph, 2)
                    ws[f"V{row_idx}"] = check_sadzby

                    if abs(check_sadzby) > 0.01:
                        ws[f"O{row_idx}"] = "Chyba"

        widths = {
            "A": 34,
            "B": 10,
            "C": 18,
            "D": 18,
            "E": 16,
            "F": 14,
            "G": 16,
            "H": 16,
            "I": 16,
            "J": 45,
            "K": 14,
            "L": 16,
            "M": 16,
            "N": 16,
            "O": 14,
            "P": 55,
            "Q": 80,
            "R": 55,
            "S": 70,
            "T": 100,
            "U": 120,
            "V": 18,
        }

        for col, width in widths.items():
            ws.column_dimensions[col].width = width

        for col_letter in ["E", "F", "G", "H", "I", "L", "M", "N", "V"]:
            for cell in ws[col_letter][1:]:
                cell.number_format = '#,##0.00 €'


def process_file(file_path: Path):
    pages = load_input_file(file_path)
    rows = []
    receipt_counter = 1

    for page_number, page_img in pages:
        h, w = page_img.shape[:2]

        direct_image_input = file_path.suffix.lower() in {
            ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"
        }

        if direct_image_input:
            # Pri samostatnom JPG/PNG je vstup už jeden bloček.
            # Nepoužívame agresívnu detekciu výrezov, lebo môže odrezať ľavú/pravú stranu.
            boxes = [(0, 0, w, h)]
        else:
            boxes = detect_receipts(page_img)

        for box in boxes:
            x1, y1, x2, y2 = box
            h, w = page_img.shape[:2]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            receipt_img = page_img[y1:y2, x1:x2]
            if receipt_img.size == 0:
                continue
            # DEBUG: uloz presny obrazok, ktory ide do OCR
            debug_dir = Path("debug_ocr_images")
            debug_dir.mkdir(exist_ok=True)
            safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", file_path.stem)
            debug_img_path = debug_dir / f"{safe_stem}_doklad_{receipt_counter:03d}_ocr_input.jpg"
            try:
                if hasattr(receipt_img, "shape") and len(receipt_img.shape) == 3:
                    cv2.imwrite(str(debug_img_path), cv2.cvtColor(receipt_img, cv2.COLOR_RGB2BGR))
                else:
                    cv2.imwrite(str(debug_img_path), receipt_img)
            except Exception:
                pass

            ocr_text = run_ocr(receipt_img)
            row = parse_receipt_text(ocr_text, receipt_counter, file_path.name)

            ocr_preview = ocr_text.replace("\r", " ").replace("\n", " | ")
            ocr_preview = " ".join(ocr_preview.split())
            row["ocrTextPreview"] = ocr_preview[:1200]
            if is_valid_receipt_row(row):
                rows.append(row)
                receipt_counter += 1
            else:
                rows.append(make_not_found_row(file_path.name))

    if not rows:
        rows.append(make_not_found_row(file_path.name))

    return rows


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Použitie: ocr_process.py <vstup> <excel_vystup>"}))
        sys.exit(1)

    input_path = Path(sys.argv[1])
    excel_path = Path(sys.argv[2])

    if not input_path.exists():
        print(json.dumps({"error": f"Súbor nenájdený: {input_path}"}))
        sys.exit(1)

    rows = process_file(input_path)
    save_excel(rows, excel_path)

    valid = sum(1 for r in rows if r.get("stav") == "OK")
    result = {
        "rows": rows,
        "totalReceipts": len(rows),
        "validReceipts": valid,
    }
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
