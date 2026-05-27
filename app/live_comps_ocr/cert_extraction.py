import json
import base64
import time
import logging
import random
import re
from pathlib import Path

from google import genai
from google.genai import types as genai_types

CY_PROMPT = (
    "You are helping a buyer on a live Whatnot stream evaluate a graded trading card slab. "
    "Your main job is to identify the grading company and read the certification number from that company's slab label. "
    "Supported grading companies include PSA, BGS, CGC, and SGC. Each company uses a different label layout and visual style. "
    "First determine the grading company. Then read the cert number from the correct label region for that company. "
    "Do not force a PSA-style read onto a non-PSA slab. "
    "Focus on the slab label area only. Ignore streamer overlays, chat, listing titles, price graphics, and non-card UI. "
    "Do not guess or hallucinate digits. If the cert number is not clearly readable, return an empty string. "
    "Normalize the cert number to digits only when possible, with no spaces or punctuation. "
    "If the screenshot is not clearly a graded slab, set is_graded_slab to false, grading_company to unknown, and cert_number to an empty string. "
    "Also classify the broad category when possible: soccer, basketball, hockey, pokemon, one_piece, or unknown. "
    "Return JSON only with this exact shape: "
    '{"mode": "cy", "is_graded_slab": bool, "grading_company": str, "cert_number": str, "player": str, "year": str, '
    '"set": str, "grade": str, "category": str, "confidence": str, "label_text": str}. '
    "grading_company is required and must be one of: PSA, BGS, CGC, SGC, unknown. "
    "confidence must be one of: high, medium, low. "
    "label_text should contain the raw label text you can read, or an empty string if unreadable."
)

MARKET_SCAN_PROMPT = (
    "You are helping a buyer on a live auction page build an eBay sold-comps search query for a trading card. "
    "Identify the card as specifically as possible from the screenshot. Focus on the foreground card only, not background cards or stream clutter. "
    "Read the player, year, set, card number, parallel, insert/subset name, grading company, grade, and cert number if visible. "
    "Do not guess details that are not visible. Prefer omission over hallucination. "
    "If a field is unclear, return an empty string rather than making it up. "
    "Then build a search query tailored for eBay sold comps / SerpAPI input using the strongest exact identifiers first. "
    "Ignore streamer overlays, chat, listing titles, price graphics, and non-card UI. "
    "Return JSON only with this exact shape: "
    '{"mode": "ebay", "grading_company": str, "cert_number": str, "player": str, "year": str, '
    '"set": str, "card_number": str, "parallel": str, "subset": str, "grade": str, "confidence": str, "query_ebay": str, "label_text": str}. '
    "grading_company must be one of: PSA, BGS, CGC, SGC, raw, unknown. "
    "confidence must be one of: high, medium, low. "
    "query_ebay should be concise and optimized for sold listing search, favoring exact identifiers like card number, parallel, and grade when known."
)


class TemporaryModelUnavailable(RuntimeError):
    pass


class ModelQuotaExceeded(RuntimeError):
    pass


class ModelResponseParseError(RuntimeError):
    pass


def _is_quota_error(error: Exception) -> bool:
    text = str(error).lower()
    return (
        '429' in text
        or 'resource_exhausted' in text
        or 'quota' in text
        or 'rate-limit' in text
        or 'rate limit' in text
    )


def _is_temporary_model_error(error: Exception) -> bool:
    text = str(error).lower()
    return (
        '503' in text
        or 'unavailable' in text
        or 'high demand' in text
        or 'temporarily' in text
        or 'try again later' in text
    )


def _generate_with_retry(gclient: genai.Client, image_bytes: bytes, prompt: str, mime_type: str, mode: str):
    instruction = 'Return JSON only.'
    delays = [0.8, 1.6, 3.2]
    last_error = None

    for attempt in range(len(delays) + 1):
        try:
            return gclient.models.generate_content(
                model='gemini-2.5-flash',
                contents=[
                    prompt + '\n\n' + instruction,
                    genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                ],
                config=genai_types.GenerateContentConfig(
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                    max_output_tokens=220 if mode == 'cy' else 300,
                ),
            )
        except Exception as error:
            last_error = error
            if _is_quota_error(error):
                raise ModelQuotaExceeded(
                    'Gemini quota exhausted for this API key. Wait for quota reset or use a paid/increased-quota key.'
                ) from error
            if not _is_temporary_model_error(error) or attempt >= len(delays):
                break

            delay = delays[attempt] + random.uniform(0, 0.3)
            logging.info(f"[gemini retry] temporary error on attempt {attempt + 1}; waiting {delay:.1f}s")
            time.sleep(delay)

    if last_error and _is_temporary_model_error(last_error):
        raise TemporaryModelUnavailable(
            'Gemini is temporarily overloaded. Wait a few seconds and scan again.'
        ) from last_error
    raise last_error


def _strip_json_fence(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[1] if '\n' in raw else raw[3:]
        if raw.endswith('```'):
            raw = raw[:-3]
        raw = raw.strip()
    return raw


def _extract_json_object(raw: str) -> str:
    start = raw.find('{')
    end = raw.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return raw
    return raw[start:end + 1]


def _partial_json_string(raw: str, key: str) -> str:
    # Salvage early model fields when the response is truncated mid-string later.
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]*)', raw)
    return match.group(1).strip() if match else ''


def _partial_json_bool(raw: str, key: str, default: bool = False) -> bool:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*(true|false)', raw, re.IGNORECASE)
    if not match:
        return default
    return match.group(1).lower() == 'true'


def _parse_model_json(raw: str, mode: str) -> dict:
    cleaned = _strip_json_fence(raw)
    candidate = _extract_json_object(cleaned)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as error:
        logging.info(f'[gemini json parse] {error}; raw={cleaned[:500]!r}')

        if mode == 'cy':
            company = _partial_json_string(candidate, 'grading_company').upper()
            cert = ''.join(ch for ch in _partial_json_string(candidate, 'cert_number') if ch.isdigit())
            if company or cert:
                return {
                    'mode': 'cy',
                    'is_graded_slab': _partial_json_bool(candidate, 'is_graded_slab', bool(cert)),
                    'grading_company': company or 'unknown',
                    'cert_number': cert,
                    'player': _partial_json_string(candidate, 'player'),
                    'year': _partial_json_string(candidate, 'year'),
                    'set': _partial_json_string(candidate, 'set'),
                    'grade': _partial_json_string(candidate, 'grade'),
                    'category': _partial_json_string(candidate, 'category'),
                    'confidence': _partial_json_string(candidate, 'confidence') or 'low',
                    'label_text': _partial_json_string(candidate, 'label_text'),
                }

        raise ModelResponseParseError(
            'Gemini returned an incomplete response. Please scan again.'
        ) from error


def resize_screenshot(screenshot_b64: str, max_width: int = 900) -> tuple[bytes, str]:
    import io
    import PIL.Image

    img_bytes = base64.b64decode(screenshot_b64)
    img = PIL.Image.open(io.BytesIO(img_bytes))
    if img.width > max_width:
        ratio = max_width / img.width
        new_size = (max_width, int(img.height * ratio))
        img = img.resize(new_size, PIL.Image.LANCZOS)
    buf = io.BytesIO()
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    img.save(buf, format='JPEG', quality=72, optimize=True)
    return buf.getvalue(), 'image/jpeg'


def identify_card_sync(gclient: genai.Client, screenshot_b64: str, mode: str = 'cy') -> dict:
    if ',' in screenshot_b64[:100]:
        screenshot_b64 = screenshot_b64.split(',', 1)[1]

    t0 = time.time()
    image_bytes, mime_type = resize_screenshot(screenshot_b64)
    logging.info(f"[resize]   {time.time()-t0:.2f}s  ({len(image_bytes)//1024}KB)")

    t1 = time.time()
    prompt = CY_PROMPT if mode == 'cy' else MARKET_SCAN_PROMPT
    response = _generate_with_retry(gclient, image_bytes, prompt, mime_type, mode)
    logging.info(f"[gemini]   {time.time()-t1:.2f}s")

    raw = response.text.strip()
    result = _parse_model_json(raw, mode)
    result['mode'] = mode
    cert = ''.join(ch for ch in str(result.get('cert_number', '') or '').strip() if ch.isdigit())
    result['cert_number'] = cert
    label_text = str(result.get('label_text', '') or '').strip()
    result['label_text'] = label_text
    result['confidence'] = str(result.get('confidence', 'low') or 'low').strip().lower()

    company = str(result.get('grading_company', 'unknown') or 'unknown').strip().upper()
    label_upper = label_text.upper()
    if company not in {'PSA', 'BGS', 'CGC', 'SGC'}:
        if 'PSA' in label_upper:
            company = 'PSA'
        elif 'BGS' in label_upper or 'BECKETT' in label_upper:
            company = 'BGS'
        elif 'CGC' in label_upper:
            company = 'CGC'
        elif 'SGC' in label_upper:
            company = 'SGC'
        else:
            company = 'unknown'
    result['grading_company'] = company
    result['card_number'] = str(result.get('card_number', '') or '').strip()
    result['parallel'] = str(result.get('parallel', '') or '').strip()
    result['subset'] = str(result.get('subset', '') or '').strip()
    result['category'] = str(result.get('category', '') or '').strip().lower()

    logging.info(f"[identified] mode={mode} company={company} cert={cert or '?'} player={result.get('player','?')} confidence={result.get('confidence','low')}")
    return result


def identify_cert_sync(gclient: genai.Client, screenshot_b64: str) -> dict:
    return identify_card_sync(gclient, screenshot_b64, mode='cy')
