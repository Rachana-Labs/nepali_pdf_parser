import logging

import fitz

logger = logging.getLogger(__name__)

TIE_EPSILON = 0.5
SMALL_FONT_RATIO = 0.22
LARGE_FONT_RATIO = 0.29
SMALL_SIZE = 9.0
LARGE_SIZE = 18.0


def _gap_threshold_for_size(font_size):
    t = (font_size - SMALL_SIZE) / (LARGE_SIZE - SMALL_SIZE)
    t = max(0.0, min(1.0, t))
    return SMALL_FONT_RATIO + t * (LARGE_FONT_RATIO - SMALL_FONT_RATIO)


def _majority_font_key(glyphs):
    if not glyphs:
        return None
    counts = {}
    for g in glyphs:
        key = g.get("font_key")
        counts[key] = counts.get(key, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _reconstruct_line_glyphs(glyphs):
    if not glyphs:
        return [], []

    glyphs_sorted_by_x = sorted(glyphs, key=lambda g: g["x0"])
    clusters = []
    current_cluster = [glyphs_sorted_by_x[0]]
    for g in glyphs_sorted_by_x[1:]:
        if abs(g["x0"] - current_cluster[-1]["x0"]) <= TIE_EPSILON:
            current_cluster.append(g)
        else:
            clusters.append(current_cluster)
            current_cluster = [g]
    clusters.append(current_cluster)

    for cluster in clusters:
        cluster.sort(key=lambda g: g["stream_index"])

    words = []
    word_fonts = []
    current_word = []
    current_word_glyphs = []
    prev_glyph = None

    for cluster in clusters:
        cluster_text = "".join(g["char"] for g in cluster)
        first_glyph = cluster[0]
        last_glyph = max(cluster, key=lambda g: g["x1"])
        is_zero_width = (last_glyph["x1"] - first_glyph["x0"]) <= 0.05

        if prev_glyph is None:
            current_word.append(cluster_text)
            current_word_glyphs.extend(cluster)
            prev_glyph = last_glyph
            continue

        if is_zero_width:
            current_word.append(cluster_text)
            current_word_glyphs.extend(cluster)
            continue

        prev_width = max(prev_glyph["x1"] - prev_glyph["x0"], 1.0)
        gap = first_glyph["x0"] - prev_glyph["x1"]
        font_size = prev_glyph.get("size") or first_glyph.get("size") or 12.0
        threshold = _gap_threshold_for_size(font_size)

        if gap > prev_width * threshold:
            words.append("".join(current_word))
            word_fonts.append(_majority_font_key(current_word_glyphs))
            current_word = [cluster_text]
            current_word_glyphs = list(cluster)
        else:
            current_word.append(cluster_text)
            current_word_glyphs.extend(cluster)

        prev_glyph = last_glyph

    if current_word:
        words.append("".join(current_word))
        word_fonts.append(_majority_font_key(current_word_glyphs))

    filtered = [(w, f) for w, f in zip(words, word_fonts) if w.strip()]
    if not filtered:
        return [], []
    words, word_fonts = zip(*filtered)
    return list(words), list(word_fonts)


class FontGlyph:
    def __init__(self, char, x0, x1, size, stream_index, font_key):
        self.char = char
        self.x0 = x0
        self.x1 = x1
        self.size = size
        self.stream_index = stream_index
        self.font_key = font_key


def page_has_text(page):
    text = page.get_text("text")
    return bool(text and text.strip())


def extract_spans(page):
    text_dict = page.get_text("rawdict")
    spans = []
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                spans.append((block, line, span))
    return spans


def extract_line_text(line, match_fn):
    nepali_glyphs = []
    other_text_parts = []
    fonts_seen = set()
    unmapped_fonts = set()
    stream_idx = 0

    for span in line.get("spans", []):
        raw_font_name = span.get("font", "unknown")
        font_size = span.get("size", 12.0)
        matched_key = match_fn(raw_font_name)

        span_text = span.get("text", "")
        if matched_key is None and span_text.strip():
            logger.warning(
                "Unknown font '%s' — passing span through unconverted",
                raw_font_name,
            )
            unmapped_fonts.add(raw_font_name)

        for ch in span.get("chars", []):
            char = ch["c"]
            if ord(char) < 0x20 or ord(char) == 0x7F:
                stream_idx += 1
                continue

            if matched_key is not None:
                if char == " ":
                    stream_idx += 1
                    continue
                fonts_seen.add(raw_font_name)
                nepali_glyphs.append({
                    "char": char,
                    "x0": ch["bbox"][0],
                    "x1": ch["bbox"][2],
                    "size": font_size,
                    "stream_index": stream_idx,
                    "font_key": matched_key,
                })
            else:
                other_text_parts.append(char)

            stream_idx += 1

    nepali_words, nepali_word_fonts = _reconstruct_line_glyphs(nepali_glyphs)
    nepali_text = " ".join(nepali_words)
    other_text = "".join(other_text_parts)

    return nepali_text, other_text, fonts_seen, unmapped_fonts, nepali_words, nepali_word_fonts
