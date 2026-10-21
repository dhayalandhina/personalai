#!/usr/bin/env python3
"""build_thanglish.py — (re)builds dd's Thanglish -> Tamil word list (skills/voice/assets/thanglish_ta.tsv).

Each word gets Google Input Tools' first Tamil suggestion, then OVERRIDES fixes the places where that first pick
is not the colloquial word dd means ("pannu" -> பானு (a name) instead of பண்ணு, "seri" -> சேரி instead of சரி...).
English words dd mixes in stay English and are not listed (english_words.txt.gz: common English words from
wordfreq, minus the Tamil list).

  python3 build_thanglish.py [--add words.txt]      re-fetch every listed word (+ new ones), rewrite the TSV
  python3 build_thanglish.py --english              rebuild english_words.txt.gz (pip install wordfreq)
"""
from __future__ import annotations

import concurrent.futures as cf
import gzip
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "assets"
TSV = ASSETS / "thanglish_ta.tsv"
ENGLISH = ASSETS / "english_words.txt.gz"
OVERRIDES = {
    "aama": "ஆமா", "ah": "ஆ", "aacha": "ஆச்சா", "aagama": "ஆகாம", "aaga": "ஆக", "da": "டா", "pa": "பா", "ma": "மா",
    "irukala": "இருக்கல", "irukkala": "இருக்கல", "kai": "கை", "kamiya": "கம்மியா", "kekuma": "கேக்குமா",
    "nadakum": "நடக்கும்", "naalaki": "நாளைக்கி", "paathaen": "பாத்தேன்", "paati": "பாட்டி", "pannu": "பண்ணு",
    "pannalla": "பண்ணல", "pannatta": "பண்ணட்டா", "pannaava": "பண்ணவா", "pannitaen": "பண்ணிட்டேன்",
    "pannittiya": "பண்ணிட்டியா", "panraanga": "பண்றாங்க", "pathu": "பத்து", "poitu": "போயிட்டு", "poidu": "போயிடு",
    "seri": "சரி", "seringa": "சரிங்க", "sariya": "சரியா", "thira": "திற", "ukku": "உக்கு", "vaaren": "வாரேன்",
    "vaati": "வாட்டி", "varuveengala": "வருவீங்களா", "inthe": "இந்த", "idha": "இத", "atha": "அத", "ullae": "உள்ளே",
    "paatha": "பாத்த", "maathu": "மாத்து", "irupadhu": "இருபது", "pannunum": "பண்ணணும்", "sollunum": "சொல்லணும்",
    "peru": "பேரு", "veetu": "வீட்டு", "kettaen": "கேட்டேன்", "mazhaiya": "மழையா", "pannidava": "பண்ணிடவா",
    "purinjucha": "புரிஞ்சுச்சா", "dhina": "தினா", "poi": "போய்", "paakava": "பாக்கவா", "yaaravadhu": "யாராவது",
    "paatukku": "பாட்டுக்கு", "pakkathu": "பக்கத்து", "mudinjadhu": "முடிஞ்சது", "aen": "ஏன்", "kodu": "கொடு",
    "valluvar": "வள்ளுவர்", "pallavi": "பல்லவி",
}
SKIP = {"um", "per"}  # also everyday English: left to the English list
HEADER = ("# Thanglish word -> Tamil script, for dd's Tamil voices. Built with skills/voice/scripts/build_thanglish.py:\n"
          "# Google Input Tools' first suggestion, hand-corrected where colloquial Tamil differs (pannu, seri, aama, da, pa...).\n")


def lookup(word: str) -> list[str]:
    url = "https://inputtools.google.com/request?" + urllib.parse.urlencode(
        {"text": word, "itc": "ta-t-i0-und", "num": 3, "cp": 0, "cs": 1, "ie": "utf-8", "oe": "utf-8"})
    ctx = None
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    for k in range(4):
        try:
            with urllib.request.urlopen(url, timeout=15, context=ctx) as r:
                d = json.loads(r.read().decode())
            if d[0] == "SUCCESS":
                return d[1][0][1]
        except Exception:
            time.sleep(1 + k)
    return []


def read_tsv() -> dict[str, str]:
    out = {}
    for line in TSV.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "\t" in line:
            w, t = line.split("\t", 1)
            out[w] = t
    return out


def build(extra: list[str]) -> int:
    words = sorted((set(read_tsv()) | {w.lower() for w in extra if w.isalpha()}) - SKIP)
    got: dict[str, str] = {}
    with cf.ThreadPoolExecutor(8) as ex:
        for w, cands in zip(words, ex.map(lookup, words)):
            if cands:
                got[w] = cands[0]
    got.update(OVERRIDES)
    TSV.write_text(HEADER + "".join(f"{w}\t{t}\n" for w, t in sorted(got.items())), encoding="utf-8")
    print(f"{TSV}: {len(got)} words ({len(words) - len(set(got) & set(words))} not found)")
    return 0


def build_english(n: int = 50000) -> int:
    from wordfreq import top_n_list
    tamil = set(read_tsv())
    eng = [w for w in top_n_list("en", n) if w.isascii() and w.isalpha() and w not in tamil]
    with gzip.open(ENGLISH, "wt", encoding="utf-8") as f:
        f.write("\n".join(eng) + "\n")
    print(f"{ENGLISH}: {len(eng)} words")
    return 0


if __name__ == "__main__":
    if "--english" in sys.argv:
        sys.exit(build_english())
    extra: list[str] = []
    if "--add" in sys.argv:
        extra = Path(sys.argv[sys.argv.index("--add") + 1]).read_text().split()
    sys.exit(build(extra))
