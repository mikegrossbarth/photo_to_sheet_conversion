from __future__ import annotations

import base64
import io
import json
import logging
import random
import re
import time

from google import genai
from google.genai import types as genai_types

from live_comps_ocr.cert_extraction import (
    ModelQuotaExceeded,
    ModelResponseParseError,
    TemporaryModelUnavailable,
    _extract_json_object,
    _is_quota_error,
    _is_temporary_model_error,
    _parse_model_json,
    _strip_json_fence,
)
from sport_lookup import lookup_sport


GRADE_WORDS_RE = re.compile(
    r"\b(?:GEM[-\s]?MT|MINT|NM[-\s]?MT\+?|NM|EX[-\s]?MT|EX|VG[-\s]?EX|VG|GOOD|FR|PR|HALF[-\s]?POINT)\b",
    re.IGNORECASE,
)
NUMERIC_GRADE_RE = re.compile(r"\d+(?:\.\d+)?")


MULTI_CARD_PROMPT = (
    "You are extracting an inventory spreadsheet from a photo that may contain one or many graded trading card slabs. "
    "Find every clearly visible graded slab/card in the image. Treat each distinct slab as one result. "
    "For each slab, read the grading company, certification number, player or subject, year, set, card number, parallel, subset, grade, category, and raw label text. "
    "Supported grading companies include PSA, BGS, CGC, SGC, and TAG. Use unknown if unclear. "
    "Work systematically left-to-right and top-to-bottom. Include a position label such as top left, top center, middle right, bottom left, or single. "
    "Focus on the slab labels and card identifiers. Ignore background objects, table texture, hands, price stickers, and unrelated text. "
    "Do not hallucinate details. If a field is unclear, return an empty string. "
    "Normalize certification numbers to digits only when possible, with no spaces or punctuation. "
    "Important: if visible graded slabs or slab-like card holders are present, return one card object for each visible slab even when the label is blurry or the cert number is unreadable. "
    "Use blank fields and low confidence for unreadable labels, but do not return an empty cards array when slabs are visibly present. "
    "Only return an empty cards array when there are truly no visible card slabs or graded-card holders. "
    "Return JSON only with this exact shape: "
    '{"cards":[{"card_index": int, "position": str, "is_graded_slab": bool, "grading_company": str, '
    '"cert_number": str, "player": str, "year": str, "set": str, "card_number": str, '
    '"parallel": str, "subset": str, "grade": str, "category": str, "confidence": str, "label_text": str}]}. '
    "confidence must be one of: high, medium, low."
)

DETECTION_PROMPT = (
    "Locate every visible graded trading card slab or slab-like card holder in this photo. "
    "This is only a detection task: do not identify players or read certification numbers. "
    "Return one object per visible slab, including partial slabs when enough of the slab/card is visible to inventory it. "
    "Detect slabs from PSA, BGS/Beckett, SGC, CGC, TAG, and unknown grading companies equally. "
    "Never group two adjacent slabs into one bounding box. If two plastic slabs touch edges, they are still two separate slabs. "
    "For a 2 by 2 grid of slabs, return exactly 4 separate boxes. For a row of 2 slabs, return 2 separate boxes. "
    "Work left-to-right and top-to-bottom. "
    "For each slab, return a bounding box around the entire slab/card holder, not just the label. "
    "Use normalized integer coordinates from 0 to 1000 with [x_min, y_min, x_max, y_max]. "
    "Return JSON only with this exact shape: "
    '{"cards":[{"card_index": int, "position": str, "bbox": [int, int, int, int], "confidence": str}]}. '
    "confidence must be high, medium, or low."
)

CROP_CARD_PROMPT = (
    "You are reading one cropped graded trading card slab/card holder that came from a larger group photo. "
    "The crop may be blurry, tilted, partial, or low resolution. Your job is to extract any visible inventory fields without guessing. "
    "If the slab/card holder is visible but label text is unreadable, keep is_graded_slab true, set confidence low, and leave unreadable fields blank. "
    "Do not reject a crop just because the cert number cannot be read. "
    "Read the grading company, certification number, player or subject, year, set, card number, parallel, subset, grade, broad category, and raw label/card text. "
    "Also preserve descriptor lines and attributes printed below or near the player/subject name on the slab label, such as GRAY BACK, REFRACTOR, SILVER, AUTO, ROOKIE, EX+, MK, OC, PD, qualifier notes, variation names, insert names, or other label details. "
    "Put those extra descriptor details in attributes as a concise semicolon-separated string. Do not drop them even if they do not fit player, set, card_number, parallel, subset, or grade. "
    "Supported grading companies include PSA, BGS, CGC, SGC, TAG, and unknown. "
    "Carefully distinguish grading company by label style and visible text: PSA labels are usually red/white with PSA logo; "
    "BGS/Beckett labels often show BGS, Beckett, subgrades, or a numeric grade box; "
    "SGC labels often have black tuxedo-style holders or green/white SGC label branding; "
    "CGC labels often show CGC text/logo and blue/green/white certification styling. "
    "Do not mark a card PSA just because the slab label is red; read visible company text or use unknown when uncertain. "
    "Normalize cert_number to digits only when possible. "
    "Only include a player/year/set/grade when the text is actually visible or nearly certain from the crop; prefer blanks over hallucination. "
    "Return JSON only with this exact shape: "
    '{"mode": "crop", "is_graded_slab": bool, "grading_company": str, "cert_number": str, "player": str, "year": str, '
    '"set": str, "card_number": str, "parallel": str, "subset": str, "attributes": str, "grade": str, "category": str, "confidence": str, "label_text": str}. '
    "confidence must be one of: high, medium, low."
)

FALLBACK_MULTI_CARD_PROMPT = (
    "This image may contain multiple graded trading card slabs. Your first priority is to create one inventory row per visible slab/card holder. "
    "Do not require readable labels. Count every visible slab-like holder, moving left-to-right and top-to-bottom. "
    "For each visible slab, fill any readable fields, and leave unreadable fields blank. "
    "Return JSON only with this exact shape: "
    '{"cards":[{"card_index": int, "position": str, "is_graded_slab": bool, "grading_company": str, '
    '"cert_number": str, "player": str, "year": str, "set": str, "card_number": str, '
    '"parallel": str, "subset": str, "grade": str, "category": str, "confidence": str, "label_text": str}]}.'
)


def _generate_with_retry(gclient: genai.Client, image_bytes: bytes, mime_type: str, prompt: str = MULTI_CARD_PROMPT):
    delays = [0.8, 1.6, 3.2]
    last_error = None
    for attempt in range(len(delays) + 1):
        try:
            return gclient.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    prompt + "\n\nReturn JSON only.",
                    genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                ],
                config=genai_types.GenerateContentConfig(
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                    max_output_tokens=1800,
                    response_mime_type="application/json",
                    temperature=0,
                ),
            )
        except Exception as error:
            last_error = error
            if _is_quota_error(error):
                raise ModelQuotaExceeded(
                    "Gemini quota exhausted for this API key. Wait for quota reset or use a paid/increased-quota key."
                ) from error
            if not _is_temporary_model_error(error) or attempt >= len(delays):
                break
            delay = delays[attempt] + random.uniform(0, 0.3)
            logging.info(f"[gemini retry] temporary error on attempt {attempt + 1}; waiting {delay:.1f}s")
            time.sleep(delay)

    if last_error and _is_temporary_model_error(last_error):
        raise TemporaryModelUnavailable("Gemini is temporarily overloaded. Wait a few seconds and scan again.") from last_error
    raise last_error


def _prepare_image(image_b64: str, max_width: int = 1800) -> tuple[bytes, str]:
    import io
    import PIL.Image

    if "," in image_b64[:100]:
        image_b64 = image_b64.split(",", 1)[1]
    img_bytes = base64.b64decode(image_b64)
    img = PIL.Image.open(io.BytesIO(img_bytes))
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), PIL.Image.LANCZOS)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88, optimize=True)
    return buf.getvalue(), "image/jpeg"


def _parse_cards(raw: str) -> list[dict]:
    cleaned = _strip_json_fence(raw)
    candidate = _extract_json_object(cleaned)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as error:
        logging.info(f"[multi-card json parse] {error}; raw={cleaned[:700]!r}")
        raise ModelResponseParseError("Gemini returned an incomplete multi-card response. Please scan again.") from error

    if isinstance(parsed, list):
        cards = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("cards"), list):
        cards = parsed["cards"]
    elif isinstance(parsed, dict):
        cards = [parsed]
    else:
        cards = []
    return [_normalize_card(card, index + 1) for index, card in enumerate(cards) if isinstance(card, dict)]


def _parse_regions(raw: str) -> list[dict]:
    cleaned = _strip_json_fence(raw)
    candidate = _extract_json_object(cleaned)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as error:
        logging.info(f"[region json parse] {error}; raw={cleaned[:700]!r}")
        raise ModelResponseParseError("Gemini returned an incomplete region-detection response. Please scan again.") from error

    raw_cards = parsed.get("cards", []) if isinstance(parsed, dict) else parsed
    if not isinstance(raw_cards, list):
        return []

    regions = []
    for index, card in enumerate(raw_cards):
        if not isinstance(card, dict):
            continue
        bbox = card.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        try:
            x1, y1, x2, y2 = [int(float(value)) for value in bbox]
        except (TypeError, ValueError):
            continue
        x1, y1, x2, y2 = [max(0, min(1000, value)) for value in (x1, y1, x2, y2)]
        if x2 - x1 < 25 or y2 - y1 < 25:
            continue
        regions.append({
            "card_index": index + 1,
            "position": str(card.get("position", "") or "").strip(),
            "bbox": [x1, y1, x2, y2],
            "detection_confidence": str(card.get("confidence", "") or "").strip().lower() or "low",
        })

    regions = _split_wide_regions(regions)
    regions = _dedupe_regions(regions)
    regions = _fill_simple_grid_gaps(regions)
    regions.sort(key=lambda item: (item["bbox"][1] // 120, item["bbox"][0]))
    for index, region in enumerate(regions):
        region["card_index"] = index + 1
    return regions[:24]


def _split_wide_regions(regions: list[dict]) -> list[dict]:
    split_regions: list[dict] = []
    for region in regions:
        x1, y1, x2, y2 = region["bbox"]
        width = x2 - x1
        height = y2 - y1
        if width > height * 1.55 and width > 260:
            mid = (x1 + x2) // 2
            left = {**region, "bbox": [x1, y1, mid, y2], "position": "left section"}
            right = {**region, "bbox": [mid, y1, x2, y2], "position": "right section"}
            split_regions.extend([left, right])
        else:
            split_regions.append(region)
    return split_regions


def _center(region: dict) -> tuple[float, float]:
    x1, y1, x2, y2 = region["bbox"]
    return ((x1 + x2) / 2, (y1 + y2) / 2)


def _cluster_centers(values: list[float], tolerance: float = 115) -> list[float]:
    centers: list[float] = []
    for value in sorted(values):
        for index, center in enumerate(centers):
            if abs(value - center) <= tolerance:
                centers[index] = (center + value) / 2
                break
        else:
            centers.append(value)
    return centers


def _nearest(value: float, centers: list[float]) -> int:
    return min(range(len(centers)), key=lambda index: abs(value - centers[index]))


def _fill_simple_grid_gaps(regions: list[dict]) -> list[dict]:
    if len(regions) not in {3, 5}:
        return regions

    x_centers = _cluster_centers([_center(region)[0] for region in regions])
    y_centers = _cluster_centers([_center(region)[1] for region in regions])
    if len(x_centers) != 2 or len(y_centers) != 2:
        return regions

    cells: dict[tuple[int, int], dict] = {}
    for region in regions:
        cx, cy = _center(region)
        cells[(_nearest(cy, y_centers), _nearest(cx, x_centers))] = region

    missing = [(row, col) for row in range(2) for col in range(2) if (row, col) not in cells]
    if len(missing) != 1:
        return regions

    row, col = missing[0]
    same_col = [region for (r, c), region in cells.items() if c == col]
    same_row = [region for (r, c), region in cells.items() if r == row]
    template = same_col[0] if same_col else same_row[0] if same_row else None
    if not template:
        return regions

    widths = [region["bbox"][2] - region["bbox"][0] for region in regions]
    heights = [region["bbox"][3] - region["bbox"][1] for region in regions]
    width = int(sorted(widths)[len(widths) // 2])
    height = int(sorted(heights)[len(heights) // 2])
    cx = x_centers[col]
    cy = y_centers[row]
    inferred = {
        **template,
        "bbox": [
            max(0, int(cx - width / 2)),
            max(0, int(cy - height / 2)),
            min(1000, int(cx + width / 2)),
            min(1000, int(cy + height / 2)),
        ],
        "position": "inferred grid cell",
        "detection_confidence": "medium",
    }
    return regions + [inferred]


def _bbox_iou(a: list[int], b: list[int]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    intersection = (ix2 - ix1) * (iy2 - iy1)
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    return intersection / max(area_a + area_b - intersection, 1)


def _dedupe_regions(regions: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for region in sorted(regions, key=lambda item: (item["bbox"][1], item["bbox"][0])):
        bbox = region["bbox"]
        if any(_bbox_iou(bbox, existing["bbox"]) > 0.42 for existing in kept):
            continue
        kept.append(region)
    return kept


def _normalize_card(card: dict, fallback_index: int) -> dict:
    result = {key: str(card.get(key, "") or "").strip() for key in (
        "position",
        "grading_company",
        "cert_number",
        "player",
        "year",
        "set",
        "card_number",
        "parallel",
        "subset",
        "attributes",
        "grade",
        "category",
        "confidence",
        "label_text",
    )}
    result["card_index"] = fallback_index
    result["is_graded_slab"] = bool(card.get("is_graded_slab", True))
    result["cert_number"] = "".join(ch for ch in result["cert_number"] if ch.isdigit())
    result["grade"] = normalize_grade(result["grade"])
    result["confidence"] = (result["confidence"] or "low").lower()
    if result["confidence"] not in {"high", "medium", "low"}:
        result["confidence"] = "low"

    company = result["grading_company"].upper()
    label_upper = result["label_text"].upper()
    if company not in {"PSA", "BGS", "CGC", "SGC", "TAG"}:
        if "PSA" in label_upper:
            company = "PSA"
        elif "BGS" in label_upper or "BECKETT" in label_upper:
            company = "BGS"
        elif "CGC" in label_upper:
            company = "CGC"
        elif "SGC" in label_upper:
            company = "SGC"
        elif "TAG" in label_upper:
            company = "TAG"
        else:
            company = "unknown"
    result["grading_company"] = company
    result["category"] = normalize_sport(result.get("category", ""), result.get("player", ""), result.get("label_text", ""))
    return _normalize_display_text(result)


def _identify_crop_sync(gclient: genai.Client, crop_b64: str) -> dict:
    image_bytes, mime_type = _prepare_image(crop_b64, max_width=1400)
    response = _generate_with_retry(gclient, image_bytes, mime_type, CROP_CARD_PROMPT)
    result = _parse_model_json(response.text.strip(), "cy")
    cert = "".join(ch for ch in str(result.get("cert_number", "") or "").strip() if ch.isdigit())
    result["cert_number"] = cert
    result["confidence"] = str(result.get("confidence", "low") or "low").strip().lower()
    result["grade"] = normalize_grade(str(result.get("grade", "") or ""))
    result["card_number"] = str(result.get("card_number", "") or "").strip()
    result["parallel"] = str(result.get("parallel", "") or "").strip()
    result["subset"] = str(result.get("subset", "") or "").strip()
    result["attributes"] = str(result.get("attributes", "") or "").strip()
    result["label_text"] = str(result.get("label_text", "") or "").strip()
    result["category"] = normalize_sport(str(result.get("category", "") or ""), str(result.get("player", "") or ""), result["label_text"])

    company = str(result.get("grading_company", "unknown") or "unknown").strip().upper()
    label_upper = result["label_text"].upper()
    if company not in {"PSA", "BGS", "CGC", "SGC", "TAG"}:
        if "PSA" in label_upper:
            company = "PSA"
        elif "BGS" in label_upper or "BECKETT" in label_upper:
            company = "BGS"
        elif "CGC" in label_upper:
            company = "CGC"
        elif "SGC" in label_upper:
            company = "SGC"
        elif "TAG" in label_upper:
            company = "TAG"
        else:
            company = "unknown"
    result["grading_company"] = company
    if result.get("is_graded_slab") is False and any(result.get(key) for key in ("grading_company", "player", "grade", "label_text")):
        result["is_graded_slab"] = True
    return _normalize_display_text(result)


def normalize_grade(value: str) -> str:
    text = GRADE_WORDS_RE.sub(" ", str(value or ""))
    numbers = NUMERIC_GRADE_RE.findall(text)
    return numbers[-1] if numbers else ""


def normalize_sport(value: str, player: str = "", label_text: str = "") -> str:
    sport = str(value or "").strip()
    if sport and sport.lower() not in {"unknown", "other", "unclear"}:
        return sport
    return lookup_sport(player, label_text)


def _normalize_display_text(result: dict) -> dict:
    upper_fields = [
        "position",
        "grading_company",
        "player",
        "year",
        "set",
        "card_number",
        "parallel",
        "subset",
        "attributes",
        "grade",
        "category",
        "confidence",
        "label_text",
        "detection_confidence",
    ]
    for key in upper_fields:
        if key in result and result.get(key) is not None:
            result[key] = str(result.get(key) or "").strip().upper()
    return result


def _decode_image(image_b64: str):
    import PIL.Image

    if "," in image_b64[:100]:
        image_b64 = image_b64.split(",", 1)[1]
    return PIL.Image.open(io.BytesIO(base64.b64decode(image_b64)))


def _crop_region_to_base64(image, bbox: list[int], padding_ratio: float = 0.045) -> str:
    x1, y1, x2, y2 = bbox
    width, height = image.size
    left = int(width * x1 / 1000)
    top = int(height * y1 / 1000)
    right = int(width * x2 / 1000)
    bottom = int(height * y2 / 1000)
    pad_x = int((right - left) * padding_ratio)
    pad_y = int((bottom - top) * padding_ratio)
    left = max(0, left - pad_x)
    top = max(0, top - pad_y)
    right = min(width, right + pad_x)
    bottom = min(height, bottom + pad_y)

    crop = image.crop((left, top, right, bottom))
    if crop.width < 900:
        scale = min(3.0, 900 / max(crop.width, 1))
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)))
    if crop.mode not in ("RGB", "L"):
        crop = crop.convert("RGB")
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _detect_regions_sync(gclient: genai.Client, image_bytes: bytes, mime_type: str) -> list[dict]:
    response = _generate_with_retry(gclient, image_bytes, mime_type, DETECTION_PROMPT)
    regions = _parse_regions(response.text.strip())
    logging.info(f"[regions detected] {len(regions)}")
    return regions


def identify_cards_sync(gclient: genai.Client, image_b64: str) -> list[dict]:
    t0 = time.time()
    image_bytes, mime_type = _prepare_image(image_b64)
    logging.info(f"[multi resize] {time.time() - t0:.2f}s ({len(image_bytes) // 1024}KB)")

    t1 = time.time()
    regions = _detect_regions_sync(gclient, image_bytes, mime_type)
    logging.info(f"[region gemini] {time.time() - t1:.2f}s")
    if regions:
        source_image = _decode_image(image_b64)
        cards = []
        for region in regions:
            try:
                crop_b64 = _crop_region_to_base64(source_image, region["bbox"])
                card = _identify_crop_sync(gclient, crop_b64)
                card["card_index"] = region["card_index"]
                card["position"] = region["position"]
                card["detection_confidence"] = region["detection_confidence"]
                cards.append(card)
            except Exception as error:
                logging.info(f"[region OCR error] card={region['card_index']} error={str(error)[:160]}")
                cards.append({
                    "card_index": region["card_index"],
                    "position": region["position"],
                    "is_graded_slab": True,
                    "grading_company": "unknown",
                    "cert_number": "",
                    "player": "",
                    "year": "",
                    "set": "",
                    "card_number": "",
                    "parallel": "",
                    "subset": "",
                    "grade": "",
                    "category": "",
                    "confidence": "low",
                    "label_text": "",
                    "detection_confidence": region["detection_confidence"],
                    "error": str(error),
                })
        logging.info(f"[multi identified via crops] {len(cards)} card(s)")
        return cards

    logging.info("[multi identified] region detection returned 0 cards; retrying whole-photo fallback prompt")
    try:
        t2 = time.time()
        response = _generate_with_retry(gclient, image_bytes, mime_type, FALLBACK_MULTI_CARD_PROMPT)
        logging.info(f"[multi fallback gemini] {time.time() - t2:.2f}s")
        cards = _parse_cards(response.text.strip())
        logging.info(f"[multi identified fallback] {len(cards)} card(s)")
        return cards
    except ModelResponseParseError:
        raise
