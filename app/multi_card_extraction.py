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
    "Yellow sticky notes, handwritten prices, glare, shadows, screen overlays, or price stickers may cover part of a slab; still return the slab if its holder or label is visible. "
    "Ignore black side bars or phone screenshot padding; they are not part of the slab layout. "
    "Detect slabs from PSA, BGS/Beckett, SGC, CGC, TAG, and unknown grading companies equally. "
    "Do not assume the cards are arranged in a grid; they may be scattered, tilted, staggered, overlapping, cropped, or in uneven rows. "
    "Never group two adjacent slabs into one bounding box. If two plastic slabs touch edges, they are still two separate slabs. "
    "Work left-to-right and top-to-bottom. "
    "For each slab, return a bounding box around the entire slab/card holder, not just the label. "
    "Use normalized integer coordinates from 0 to 1000 with [x_min, y_min, x_max, y_max]. "
    "Return JSON only with this exact shape: "
    '{"cards":[{"card_index": int, "position": str, "bbox": [int, int, int, int], "confidence": str}]}. '
    "confidence must be high, medium, or low."
)

ROW_DETECTION_PROMPT = (
    "Locate each visible graded trading card slab/card holder in this photo, with special attention to slabs standing side-by-side in a simple horizontal row. "
    "Return one box around each full plastic slab holder. Do not use grading label boxes only. Do not merge touching slabs. "
    "If three separate slab holders are visible left, middle, and right, return exactly three separate full-slab boxes. "
    "Cards may still be tilted, partially cropped, or different grading companies. Ignore background boxes, table surfaces, and handwriting. "
    "Use normalized integer coordinates from 0 to 1000 with [x_min, y_min, x_max, y_max]. "
    "Return JSON only with this exact shape: "
    '{"cards":[{"card_index": int, "position": str, "bbox": [int, int, int, int], "confidence": str}]}. '
    "confidence must be high, medium, or low."
)

LABEL_DETECTION_PROMPT = (
    "Locate every visible grading label/header area on graded card slabs in this photo. "
    "This is a detection task only. Find PSA, BGS/Beckett, SGC, CGC, TAG, and unknown slab labels equally. "
    "Cards may be in a random layout, tilted, partially visible, overlapping, or not aligned to a grid. "
    "A sticky note, handwritten price, glare, or sticker may overlap the card body; still find the label/header if it is visible. "
    "Ignore black side bars, phone screenshot padding, and app overlay icons. "
    "Return one object per visible slab label, not one object per row or group. Never merge adjacent labels. "
    "For each label, return a bounding box around the label/header region, not the whole slab. "
    "Use normalized integer coordinates from 0 to 1000 with [x_min, y_min, x_max, y_max]. "
    "Return JSON only with this exact shape: "
    '{"cards":[{"card_index": int, "position": str, "bbox": [int, int, int, int], "confidence": str}]}. '
    "confidence must be high, medium, or low."
)

LABEL_SWEEP_PROMPT = (
    "Sweep the entire image for grading label/header strips on visible trading card slabs. "
    "This is a label-finding task only. Return one box per visible slab label, even if the card image below the label is sideways, partially covered, or less visible. "
    "Include PSA red/white labels, BGS/Beckett labels, SGC labels, CGC labels, TAG labels, and unknown grading labels. "
    "Do not skip labels near the left/right image edges, labels partly touching neighboring slabs, or labels on slabs standing in a simple row. "
    "Never merge neighboring labels into one box. If three grading labels are visible across a row, return three separate label boxes. "
    "Ignore cardboard boxes, handwriting, price stickers, table/background text, and PSA logos printed at the bottom of holders. "
    "Use normalized integer coordinates from 0 to 1000 with [x_min, y_min, x_max, y_max]. "
    "Return JSON only with this exact shape: "
    '{"cards":[{"card_index": int, "position": str, "bbox": [int, int, int, int], "confidence": str}]}. '
    "confidence must be high, medium, or low."
)

CROP_CARD_PROMPT = (
    "You are reading one cropped graded trading card slab/card holder that came from a larger group photo. "
    "The crop may be blurry, tilted, partial, or low resolution. Your job is to extract any visible inventory fields without guessing. "
    "Use the main/central slab in the crop. If parts of neighboring slab labels appear at the crop edges, ignore those neighboring labels completely. "
    "Ignore handwritten prices, sticky notes, price stickers, and marker writing on sleeves; they are not inventory fields. "
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

CERT_ONLY_PROMPT = (
    "You are verifying the certification number on one cropped graded trading card slab. "
    "Only read the main/central slab label in this crop. Ignore neighboring labels, handwritten prices, sticky notes, price stickers, and marker writing. "
    "Read the grading company and the complete certification number printed on the slab label. "
    "For PSA labels, the cert number is usually at the far right or bottom-right of the label; pay special attention to the rightmost digits and do not drop a trailing digit. "
    "Do not guess. If every digit of the cert is not clearly readable, return an empty cert_number and low confidence. "
    "Return JSON only with this exact shape: "
    '{"mode":"cert_verify","grading_company":str,"cert_number":str,"confidence":str,"label_text":str}. '
    "confidence must be high, medium, or low."
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


def _prepare_image(image_b64: str, max_width: int = 2400) -> tuple[bytes, str]:
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
        position = str(card.get("position", "") or "").strip()
        regions.append({
            "card_index": index + 1,
            "position": position,
            "bbox": [x1, y1, x2, y2],
            "detection_confidence": str(card.get("confidence", "") or "").strip().lower() or "low",
        })

    regions = _orient_region_set(regions)
    regions = _dedupe_regions(regions)
    regions.sort(key=lambda item: (item["bbox"][1] // 120, item["bbox"][0]))
    for index, region in enumerate(regions):
        region["card_index"] = index + 1
    return regions[:24]


def _third(value: float) -> str:
    if value < 360:
        return "low"
    if value > 640:
        return "high"
    return "mid"


def _position_score(bbox: list[int], position: str) -> int:
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    pos = position.lower()
    score = 0
    x_third = _third(cx)
    y_third = _third(cy)
    if "left" in pos and x_third == "low":
        score += 1
    if "right" in pos and x_third == "high":
        score += 1
    if "center" in pos and x_third == "mid":
        score += 1
    if "top" in pos and y_third == "low":
        score += 1
    if "bottom" in pos and y_third == "high":
        score += 1
    if "middle" in pos and y_third == "mid":
        score += 1
    return score


def _orient_bbox(bbox: list[int], position: str) -> list[int]:
    swapped = [bbox[1], bbox[0], bbox[3], bbox[2]]
    if swapped[2] - swapped[0] < 25 or swapped[3] - swapped[1] < 25:
        return bbox
    if _position_score(swapped, position) > _position_score(bbox, position):
        return swapped
    return bbox


def _orient_region_set(regions: list[dict]) -> list[dict]:
    original_score = sum(_position_score(region["bbox"], region.get("position", "")) for region in regions)
    swapped_score = sum(
        _position_score([region["bbox"][1], region["bbox"][0], region["bbox"][3], region["bbox"][2]], region.get("position", ""))
        for region in regions
    )
    if swapped_score > original_score:
        return [
            {**region, "bbox": [region["bbox"][1], region["bbox"][0], region["bbox"][3], region["bbox"][2]]}
            for region in regions
        ]
    return [{**region, "bbox": _orient_bbox(region["bbox"], region.get("position", ""))} for region in regions]


def _expand_label_regions(label_regions: list[dict]) -> list[dict]:
    expanded = []
    for region in label_regions:
        x1, y1, x2, y2 = region["bbox"]
        width = x2 - x1
        height = y2 - y1
        if width < 40 or height < 12:
            continue
        if width > 360 or height > 180:
            continue
        vertical = height > width * 0.75
        if vertical:
            slab_width = min(max(int(width * 4.2), 170), 330)
            slab_height = min(max(int(height * 1.6), 250), 520)
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            bbox = [
                max(0, cx - slab_width // 2),
                max(0, cy - slab_height // 2),
                min(1000, cx + slab_width // 2),
                min(1000, cy + slab_height // 2),
            ]
        else:
            pad_x = max(int(width * 0.12), 14)
            slab_down = min(max(int(width * 1.35), 210), 520)
            slab_up = max(int(height * 1.1), 35)
            bbox = [
                max(0, x1 - pad_x),
                max(0, y1 - slab_up),
                min(1000, x2 + pad_x),
                min(1000, y2 + slab_down),
            ]
        if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > 185000:
            continue
        expanded.append({
            **region,
            "bbox": bbox,
            "position": region.get("position", "") or "label anchor",
            "detection_confidence": region.get("detection_confidence", "medium"),
        })
    return expanded


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
        duplicate_index = next((index for index, existing in enumerate(kept) if _bbox_iou(bbox, existing["bbox"]) > 0.42), None)
        if duplicate_index is not None:
            if _region_preference_score(region) > _region_preference_score(kept[duplicate_index]):
                kept[duplicate_index] = region
            continue
        kept.append(region)
    return kept


def _merge_regions(primary: list[dict], secondary: list[dict]) -> list[dict]:
    merged = list(primary)
    for region in secondary:
        x1, y1, x2, y2 = region["bbox"]
        area = (x2 - x1) * (y2 - y1)
        if area > 190000:
            continue
        overlap_index = next((index for index, existing in enumerate(merged) if _bbox_iou(region["bbox"], existing["bbox"]) > 0.12), None)
        if overlap_index is not None:
            if _region_preference_score(region) > _region_preference_score(merged[overlap_index]):
                merged[overlap_index] = region
            continue
        merged.append(region)
    return merged


def _region_preference_score(region: dict) -> int:
    x1, y1, x2, y2 = region["bbox"]
    width = x2 - x1
    height = y2 - y1
    area = width * height
    score = area
    if height >= 330:
        score += 70000
    if height <= 180:
        score -= 45000
    confidence = str(region.get("detection_confidence", "") or "").lower()
    if confidence == "high":
        score += 15000
    elif confidence == "low":
        score -= 8000
    return score


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
    result["attributes"] = _normalize_attributes(str(result.get("attributes", "") or ""))
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
    _verify_crop_cert(gclient, crop_b64, result)
    if result.get("is_graded_slab") is False and any(result.get(key) for key in ("grading_company", "player", "grade", "label_text")):
        result["is_graded_slab"] = True
    return _normalize_display_text(result)


def _verify_crop_cert(gclient: genai.Client, crop_b64: str, result: dict) -> None:
    cert = str(result.get("cert_number", "") or "")
    if not cert:
        result["cert_verified"] = ""
        return
    try:
        verification = _verify_cert_only_sync(gclient, crop_b64)
    except Exception as error:
        logging.info(f"[cert verification skipped] cert={cert} error={str(error)[:140]}")
        result["cert_verified"] = "UNVERIFIED"
        result["confidence"] = "medium" if result.get("confidence") == "high" else result.get("confidence", "low")
        return

    verified_cert = "".join(ch for ch in str(verification.get("cert_number", "") or "") if ch.isdigit())
    verified_company = str(verification.get("grading_company", "") or "").strip().upper()
    company = str(result.get("grading_company", "") or "").strip().upper()
    company_ok = not verified_company or verified_company == "UNKNOWN" or verified_company == company
    rotated_cert = ""
    rotated_company = ""
    if company == "PSA" and (_is_modern_short_psa_cert(result, verified_cert or cert) or verified_cert != cert):
        rotated = _verify_rotated_cert_only_sync(gclient, crop_b64)
        rotated_cert = "".join(ch for ch in str(rotated.get("cert_number", "") or "") if ch.isdigit())
        rotated_company = str(rotated.get("grading_company", "") or "").strip().upper()
        if rotated_cert == cert:
            verified_cert = rotated_cert
            verified_company = rotated_company or verified_company
            company_ok = not verified_company or verified_company == "UNKNOWN" or verified_company == company
        elif rotated_cert.startswith(verified_cert or cert) and len(rotated_cert) > len(verified_cert or cert):
            verified_cert = rotated_cert
            verified_company = rotated_company or verified_company
            company_ok = not verified_company or verified_company == "UNKNOWN" or verified_company == company
    if verified_cert.startswith(cert) and len(verified_cert) > len(cert):
        result["cert_number"] = verified_cert
        cert = verified_cert
    if verified_cert == cert and company_ok:
        result["cert_verified"] = "YES"
        return

    logging.info(f"[cert verification mismatch] crop={cert or '?'} verify={verified_cert or '?'} company={company}/{verified_company}")
    result["cert_verified"] = "NO"
    result["confidence"] = "medium" if result.get("confidence") == "high" else result.get("confidence", "low")


def _is_modern_short_psa_cert(result: dict, cert: str) -> bool:
    year = str(result.get("year", "") or "")
    return bool(re.match(r"^20(?:2[4-9]|[3-9]\d)", year) and len(cert) < 9)


def _verify_cert_only_sync(gclient: genai.Client, crop_b64: str) -> dict:
    image_bytes, mime_type = _prepare_image(crop_b64, max_width=1800)
    response = _generate_with_retry(gclient, image_bytes, mime_type, CERT_ONLY_PROMPT)
    result = _parse_model_json(response.text.strip(), "cy")
    result["cert_number"] = "".join(ch for ch in str(result.get("cert_number", "") or "") if ch.isdigit())
    result["grading_company"] = str(result.get("grading_company", "") or "").strip().upper()
    result["confidence"] = str(result.get("confidence", "low") or "low").strip().lower()
    return result


def _verify_rotated_cert_only_sync(gclient: genai.Client, crop_b64: str) -> dict:
    from PIL import Image

    if "," in crop_b64[:100]:
        crop_b64 = crop_b64.split(",", 1)[1]
    image = Image.open(io.BytesIO(base64.b64decode(crop_b64)))
    candidates = []
    for angle in (-90, 90):
        rotated = image.rotate(angle, expand=True)
        buf = io.BytesIO()
        if rotated.mode not in ("RGB", "L"):
            rotated = rotated.convert("RGB")
        rotated.save(buf, format="PNG")
        try:
            candidates.append(_verify_cert_only_sync(gclient, base64.b64encode(buf.getvalue()).decode("utf-8")))
        except Exception as error:
            logging.info(f"[rotated cert verification skipped] angle={angle} error={str(error)[:140]}")
    return max(candidates, key=lambda item: len(str(item.get("cert_number", "") or "")), default={})


def normalize_grade(value: str) -> str:
    text = GRADE_WORDS_RE.sub(" ", str(value or ""))
    numbers = NUMERIC_GRADE_RE.findall(text)
    return numbers[-1] if numbers else ""


def _normalize_attributes(value: str) -> str:
    text = GRADE_WORDS_RE.sub(" ", str(value or ""))
    parts = [re.sub(r"\s+", " ", part).strip(" ;,") for part in re.split(r"[;\n]+", text)]
    return "; ".join(part for part in parts if part)


def normalize_sport(value: str, player: str = "", label_text: str = "") -> str:
    sport = str(value or "").strip()
    if sport and sport.lower() not in {"unknown", "other", "unclear", "sports"}:
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
    if "attributes" in result:
        result["attributes"] = _normalize_attributes(result.get("attributes", "")).upper()
    return result


def _decode_image(image_b64: str):
    import PIL.Image

    if "," in image_b64[:100]:
        image_b64 = image_b64.split(",", 1)[1]
    return PIL.Image.open(io.BytesIO(base64.b64decode(image_b64)))


def _crop_region_to_base64(image, bbox: list[int], padding_ratio: float = 0.03) -> str:
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


def _detect_regions_for_prompt(gclient: genai.Client, image_bytes: bytes, mime_type: str, prompt: str) -> list[dict]:
    response = _generate_with_retry(gclient, image_bytes, mime_type, prompt)
    return _parse_regions(response.text.strip())


def _detect_best_row_regions(gclient: genai.Client, image_bytes: bytes, mime_type: str, attempts: int = 2) -> list[dict]:
    best: list[dict] = []
    for _ in range(attempts):
        try:
            candidate = _detect_regions_for_prompt(gclient, image_bytes, mime_type, ROW_DETECTION_PROMPT)
        except Exception as error:
            logging.info(f"[row detection skipped] {str(error)[:160]}")
            continue
        if len(candidate) > len(best):
            best = candidate
        if len(best) >= 3:
            break
    return best


def _detect_best_prompt_regions(gclient: genai.Client, image_bytes: bytes, mime_type: str, prompt: str, attempts: int = 2) -> list[dict]:
    best: list[dict] = []
    for _ in range(attempts):
        try:
            candidate = _detect_regions_for_prompt(gclient, image_bytes, mime_type, prompt)
        except Exception as error:
            logging.info(f"[prompt detection skipped] {str(error)[:160]}")
            continue
        if len(candidate) > len(best):
            best = candidate
        if len(best) >= 3:
            break
    return best


def _detect_regions_sync(gclient: genai.Client, image_bytes: bytes, mime_type: str) -> list[dict]:
    regions = _detect_regions_for_prompt(gclient, image_bytes, mime_type, DETECTION_PROMPT)
    row_regions = []
    if len(regions) < 4:
        row_regions = _detect_best_row_regions(gclient, image_bytes, mime_type)
    if len(row_regions) > len(regions):
        regions = row_regions
    elif row_regions:
        regions = _merge_regions(regions, row_regions)
    label_regions = []
    try:
        label_regions.extend(_detect_regions_for_prompt(gclient, image_bytes, mime_type, LABEL_DETECTION_PROMPT))
    except Exception as error:
        logging.info(f"[label detection skipped] {str(error)[:160]}")
    label_regions.extend(_detect_best_prompt_regions(gclient, image_bytes, mime_type, LABEL_SWEEP_PROMPT))
    if label_regions:
        expanded_labels = _expand_label_regions(_dedupe_regions(label_regions))
        if expanded_labels:
            if len(expanded_labels) > len(regions):
                regions = expanded_labels
            else:
                regions = _merge_regions(expanded_labels, regions)
    regions = _dedupe_regions(regions)
    if len(regions) < 3 and row_regions:
        regions = _merge_regions(regions, row_regions)
        regions = _dedupe_regions(regions)
    regions = _add_uncovered_edge_regions(regions)
    regions = _expand_body_regions_upward(regions)
    regions = _expand_partial_row_regions(regions)
    regions.sort(key=lambda item: (item["bbox"][1] // 120, item["bbox"][0]))
    for index, region in enumerate(regions):
        region["card_index"] = index + 1
    logging.info(f"[regions detected] {len(regions)}")
    return regions


def _expand_partial_row_regions(regions: list[dict]) -> list[dict]:
    row_groups = {
        "top": [region for region in regions if "top" in str(region.get("position", "")).lower()],
        "bottom": [region for region in regions if "bottom" in str(region.get("position", "")).lower()],
    }
    updated = []
    for region in regions:
        bbox = region["bbox"]
        x1, y1, x2, y2 = bbox
        height = y2 - y1
        pos = str(region.get("position", "")).lower()
        row_key = "top" if "top" in pos else "bottom" if "bottom" in pos else ""
        row = row_groups.get(row_key, [])
        tall_row_boxes = [item["bbox"] for item in row if item["bbox"][3] - item["bbox"][1] >= max(height * 1.35, 330)]
        if row_key and height < 320 and tall_row_boxes:
            row_y1 = min(item[1] for item in tall_row_boxes)
            row_y2 = max(item[3] for item in tall_row_boxes)
            bbox = [x1, row_y1, x2, row_y2]
        updated.append({**region, "bbox": bbox})
    return updated


def _add_uncovered_edge_regions(regions: list[dict]) -> list[dict]:
    if len(regions) < 2:
        return regions
    sorted_regions = sorted(regions, key=lambda item: item["bbox"][0])
    first = sorted_regions[0]["bbox"]
    tall_boxes = [region["bbox"] for region in regions if region["bbox"][3] - region["bbox"][1] >= 350]
    if not tall_boxes:
        return regions
    y1 = max(0, min(box[1] for box in tall_boxes) - 30)
    y2 = min(1000, max(max(box[3] for box in tall_boxes), max(region["bbox"][3] for region in regions)))
    added = list(regions)
    if first[0] > 260:
        added.append({
            "card_index": len(added) + 1,
            "position": "left edge candidate",
            "bbox": [0, y1, min(1000, first[0] + 15), y2],
            "detection_confidence": "low",
        })
    return added


def _expand_body_regions_upward(regions: list[dict]) -> list[dict]:
    updated = []
    for region in regions:
        x1, y1, x2, y2 = region["bbox"]
        width = x2 - x1
        height = y2 - y1
        if y1 > 180 and width >= 160 and height >= 280:
            y1 = max(0, y1 - max(220, int(height * 0.42)))
        updated.append({**region, "bbox": [x1, y1, x2, y2]})
    return updated


def _card_read_score(card: dict) -> int:
    score = 0
    cert_status = str(card.get("cert_verified", "") or "").upper()
    if card.get("cert_number") and (not cert_status or cert_status == "YES"):
        score += 45
    company = str(card.get("grading_company", "") or "").strip().lower()
    if company and company != "unknown":
        score += 15
    if card.get("player"):
        score += 15
    if card.get("year"):
        score += 10
    if card.get("set"):
        score += 10
    if card.get("grade"):
        score += 10
    confidence = str(card.get("confidence", "") or "").lower()
    score += {"high": 5, "medium": 3, "low": 0}.get(confidence, 0)
    return min(score, 100)


def _norm_key(value) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def _is_same_subject_card(a: dict, b: dict) -> bool:
    player_a = _norm_key(a.get("player"))
    player_b = _norm_key(b.get("player"))
    if not player_a or player_a != player_b:
        return False
    set_a = _norm_key(a.get("set"))
    set_b = _norm_key(b.get("set"))
    year_a = _norm_key(a.get("year"))
    year_b = _norm_key(b.get("year"))
    category_a = _norm_key(a.get("category"))
    category_b = _norm_key(b.get("category"))
    return bool((set_a and set_a == set_b) or (year_a and year_a == year_b) or (category_a and category_a == category_b))


def _dedupe_card_results(cards: list[dict]) -> list[dict]:
    useful = [
        card for card in cards
        if any(card.get(key) for key in ("cert_number", "player", "year", "set", "card_number", "parallel", "subset", "grade", "label_text"))
    ]
    ordered = sorted(enumerate(useful), key=lambda item: (-_card_read_score(item[1]), item[0]))
    kept: list[tuple[int, dict]] = []
    for original_index, card in ordered:
        cert = str(card.get("cert_number", "") or "")
        if cert and any(cert == str(existing.get("cert_number", "") or "") for _, existing in kept):
            continue
        if not cert and any(existing.get("cert_number") and _is_same_subject_card(card, existing) for _, existing in kept):
            continue
        kept.append((original_index, card))
    return [card for _, card in sorted(kept, key=lambda item: item[0])]


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
        return _dedupe_card_results(cards)

    logging.info("[multi identified] region detection returned 0 cards; retrying whole-photo fallback prompt")
    try:
        t2 = time.time()
        response = _generate_with_retry(gclient, image_bytes, mime_type, FALLBACK_MULTI_CARD_PROMPT)
        logging.info(f"[multi fallback gemini] {time.time() - t2:.2f}s")
        cards = _parse_cards(response.text.strip())
        logging.info(f"[multi identified fallback] {len(cards)} card(s)")
        return _dedupe_card_results(cards)
    except ModelResponseParseError:
        raise
