import json
import os
import re

MAP_PATH = os.path.join(os.path.dirname(__file__), "map.json")


def match_font_key(font_name, known_keys):
    """Loose substring match used by extractor.py/cli.py to decide whether
    a raw PDF span font name (e.g. 'ABCDEF+Gorkhapatra-Bold') refers to one
    of the known Nepali font keys, before any text has been converted.
    This is intentionally separate from AutoresearchMapper._resolve_font,
    which only disambiguates among fonts already confirmed to be Nepali."""
    lowered = (font_name or "").lower().replace("+", " ")
    for key in known_keys:
        key_lower = key.lower()
        if key_lower == lowered or key_lower in lowered or lowered in key_lower:
            return key
    return None


class AutoresearchMapper:
    """Converts legacy ASCII-glyph Nepali fonts (Preeti and its relatives --
    Gorkhapatra, NayaNepal, HimalayaBoldGopa, etc.) to Unicode Devanagari.

    Each font is a fully independent rule set loaded from map.json under
    map.json[font_name]["rules"] -- there is NO inheritance between fonts,
    even if one started life as a copy of another. Conversion is a
    three-stage per-token pipeline:
        pre-rules (regex, on raw ASCII, in order)
        -> character-map (1:1 substitution)
        -> post-rules (regex, on Unicode, in order)

    This ordering matters: matras like the ikar are typed in visual order
    in Preeti (before the consonant) and have to be reordered to logical
    order (after the consonant) via pre-rules BEFORE the character-map
    turns the ASCII bytes into Devanagari code points. Running character-map
    first silently breaks every pre-rule regex that targets raw ASCII
    sequences, since it then tries to match against already-converted
    Unicode text.

    Public API:
        AutoresearchMapper(map_path=MAP_PATH)       -- loads map.json
        .available_fonts                            -- list of loaded font names
        .preeti_to_unicode(text, font="Preeti")      -- convert text using that font's rules
    """

    def __init__(self, map_path=MAP_PATH):
        with open(map_path, "r", encoding="utf-8") as f:
            self._raw = json.load(f)

        self._fonts = {}
        for font_name, font_data in self._raw.items():
            rules = font_data.get("rules", {})
            self._fonts[font_name] = {
                "character_map": rules.get("character-map", {}),
                "pre_rules": self._compile_rules(rules.get("pre-rules", [])),
                "post_rules": self._compile_rules(rules.get("post-rules", [])),
            }

        self.available_fonts = list(self._fonts.keys())

    def get_known_fonts(self):
        """Returns the set of font keys loaded from map.json. cli.py uses
        this (not the available_fonts attribute) to build the match_fn
        passed into extract_line_text."""
        return set(self._fonts.keys())

    @staticmethod
    def _compile_rules(rule_list):
        """Each entry is expected as a [pattern, replacement] pair. Also
        accepts {"pattern": ..., "replacement": ...} dicts defensively,
        in case map.json entries were hand-written that way."""
        compiled = []
        for entry in rule_list:
            if isinstance(entry, dict):
                pattern, replacement = entry["pattern"], entry.get("replacement", "")
            else:
                pattern, replacement = entry[0], entry[1]
            compiled.append((re.compile(pattern), replacement))
        return compiled

    def _resolve_font(self, font):
        if font in self._fonts:
            return font

        # 1. Lowercase and strip non-alphanumeric characters
        normalize = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        target = normalize(font)

        # 2. Strip common font style modifiers so "preetibold" becomes "preeti"
        target = re.sub(r"(bold|italic|regular|medium|oblique)$", "", target)

        for name in self._fonts:
            # Also clean the registered font names just in case
            clean_name = re.sub(r"(bold|italic|regular|medium|oblique)$", "", normalize(name))
            if clean_name == target:
                return name

        raise KeyError(
            f"Font '{font}' not found in map.json. Available fonts: {self.available_fonts}"
        )

    def preeti_to_unicode(self, text, font="Preeti"):
        font_name = self._resolve_font(font)
        rules = self._fonts[font_name]

        # Split on whitespace but KEEP the whitespace tokens (including
        # newlines) so multi-line samples round-trip exactly -- only the
        # non-whitespace tokens get converted.
        parts = re.split(r"(\s+)", text)
        converted = [
            part if part == "" or part.isspace() else self._convert_token(part, rules)
            for part in parts
        ]
        result = "".join(converted)

        # Match the cleanup extractor.py already relies on downstream.
        result = result.replace('\xa0', ' ')
        result = re.sub(r'[\u200b\u200c\u200d](?=\s|।|$)', '', result)

        return result

    @staticmethod
    def _convert_token(token, rules):
        for pattern, replacement in rules["pre_rules"]:
            token = pattern.sub(replacement, token)

        token = "".join(rules["character_map"].get(ch, ch) for ch in token)

        for pattern, replacement in rules["post_rules"]:
            token = pattern.sub(replacement, token)

        return token