#!/usr/bin/env python3
"""dd_thanglish.py — dd's Thanglish replies -> Tamil script, so the Tamil Nadu voices pronounce them right.

dd writes Thanglish ("Chrome open pannitten, innum enna venum?"). The Tamil voices (Pallavi, Valluvar) read Tamil
words properly only in Tamil script and English words best as they are, so each word goes:
  a Tamil word   -> Tamil script   (a hand-checked list of ~1100 colloquial words, then phonetic rules)
  an English word -> unchanged      (~48k common English words, brand/tech names, acronyms, anything with digits)
"Chrome open pannitten" -> "Chrome open பண்ணிட்டேன்" — spoken the way people in Tamil Nadu actually talk.
Only the voice sees the Tamil script; the screen keeps the Thanglish.

Words that are neither in the list nor English get the phonetic rules at once (~85% exact on the list), and —
when online — Google Input Tools' spelling is fetched in the background for next time (~/.dd/thanglish_learned.json,
DD_TRANSLIT_LEARN=0 turns that off).

  python3 examples/dd_thanglish.py "Chrome open pannitten, innum enna venum?"
  python3 examples/dd_thanglish.py --selftest
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
import threading
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "skills" / "voice" / "assets"
DICT_FILE = ASSETS / "thanglish_ta.tsv"
ENGLISH_FILE = ASSETS / "english_words.txt.gz"
PULLI = "்"

# ---------------- word lists ----------------
_lock = threading.Lock()
_dict: dict[str, str] | None = None
_english: set[str] | None = None


def home() -> Path:
    return Path(os.environ.get("DD_HOME", Path.home() / ".dd"))


def learned_file() -> Path:
    return home() / "thanglish_learned.json"


def tamil_words() -> dict[str, str]:
    global _dict
    with _lock:
        if _dict is None:
            d: dict[str, str] = {}
            try:
                for line in DICT_FILE.read_text(encoding="utf-8").splitlines():
                    if line and not line.startswith("#") and "\t" in line:
                        w, t = line.split("\t", 1)
                        d[w.strip().lower()] = t.strip()
            except OSError:
                pass
            try:
                learned = json.loads(learned_file().read_text(encoding="utf-8"))
                d.update({k: v for k, v in learned.items() if k not in d})
            except (OSError, ValueError):
                pass
            _dict = d
        return _dict


def english_words() -> set[str]:
    global _english
    with _lock:
        if _english is None:
            try:
                with gzip.open(ENGLISH_FILE, "rt", encoding="utf-8") as f:
                    _english = {w.strip() for w in f if w.strip()}
            except OSError:
                _english = set()
        return _english


# ---------------- phonetic rules (romanised colloquial Tamil -> Tamil script) ----------------
VOWELS = [  # latin, independent letter, vowel sign (longest first)
    ("aa", "ஆ", "ா"), ("ai", "ஐ", "ை"), ("au", "ஔ", "ௌ"), ("ee", "ஈ", "ீ"), ("ii", "ஈ", "ீ"),
    ("oo", "ஊ", "ூ"), ("uu", "ஊ", "ூ"), ("ae", "ஏ", "ே"), ("oa", "ஓ", "ோ"), ("ei", "எய்", "ெய்"), ("ow", "ஔ", "ௌ"),
    ("a", "அ", ""), ("i", "இ", "ி"), ("u", "உ", "ு"), ("e", "எ", "ெ"), ("o", "ஒ", "ொ"),
]
LONG = {"எ": ("ஏ", "ே"), "ஒ": ("ஓ", "ோ")}
CONS = [  # latin -> Tamil consonants (all but the last get a pulli); longest first
    ("ndr", ["ன", "ற"]), ("nth", ["ந", "த"]), ("ndh", ["ந", "த"]), ("nch", ["ஞ", "ச"]),
    ("chch", ["ச", "ச"]), ("cch", ["ச", "ச"]), ("thth", ["த", "த"]), ("tth", ["த", "த"]), ("ddh", ["த", "த"]),
    ("zh", ["ழ"]), ("th", ["த"]), ("dh", ["த"]), ("sh", ["ஷ"]), ("ch", ["ச"]), ("nj", ["ஞ", "ச"]),
    ("ng", ["ங", "க"]), ("nk", ["ங", "க"]), ("nd", ["ண", "ட"]), ("nt", ["ண", "ட"]), ("mb", ["ம", "ப"]),
    ("kh", ["க"]), ("gh", ["க"]), ("bh", ["ப"]), ("ph", ["ஃப"]),
    ("kk", ["க", "க"]), ("gg", ["க", "க"]), ("pp", ["ப", "ப"]), ("bb", ["ப", "ப"]), ("tt", ["ட", "ட"]),
    ("dd", ["ட", "ட"]), ("mm", ["ம", "ம"]), ("ll", ["ல", "ல"]), ("yy", ["ய", "ய"]), ("vv", ["வ", "வ"]),
    ("rr", ["ற", "ற"]), ("ss", ["ச", "ச"]), ("nn", ["ன", "ன"]),
    ("k", ["க"]), ("g", ["க"]), ("c", ["க"]), ("q", ["க"]), ("x", ["க", "ஸ"]), ("s", ["ச"]), ("j", ["ஜ"]),
    ("t", ["ட"]), ("d", ["ட"]), ("p", ["ப"]), ("b", ["ப"]), ("f", ["ஃப"]), ("m", ["ம"]), ("y", ["ய"]),
    ("r", ["ர"]), ("l", ["ல"]), ("v", ["வ"]), ("w", ["வ"]), ("z", ["ஜ"]), ("h", ["ஹ"]), ("n", ["ன"]),
]
PRESENT_TENSE_TAILS = ("en", "een", "om", "oam", "aanga", "anga", "inga", "eenga", "athu", "adhu", "a", "aa", "ae")


def _tokens(word: str) -> list:
    out, i = [], 0
    while i < len(word):
        for lat, ind, sign in VOWELS:
            if word.startswith(lat, i):
                out.append(("V", lat, ind, sign))
                i += len(lat)
                break
        else:
            for lat, cs in CONS:
                if word.startswith(lat, i):
                    out.append(("C", lat, list(cs)))
                    i += len(lat)
                    break
            else:
                out.append(("X", word[i]))
                i += 1
    return out


def rules(word: str) -> str:
    """Romanised colloquial Tamil -> Tamil script by rule (pannuren -> பண்ணுறேன், konjam -> கொஞ்சம்)."""
    w = word.lower()
    lead = None
    if w.startswith(("ny", "gn")) and len(w) > 3 and w[2] in "aeiou":
        lead, w = ("C", "ny", ["ஞ"]), w[2:]
    toks = _tokens(w)
    if lead:
        toks = [lead] + toks
    toks = [t for i, t in enumerate(toks) if not (t[0] == "C" and t[1] == "h" and i and toks[i - 1][0] == "C")]
    fixed: list = []
    for t in toks:  # a glide between two vowels: aayi, poyi, seiya
        if fixed and fixed[-1][0] == "V" and t[0] == "V":
            if fixed[-1][1] == "ei":
                fixed += [("C", "~", ["ய"]), t]
                continue
            g = "வ" if fixed[-1][1] in ("u", "uu", "oo", "o") and t[1] not in ("i", "ee") else "ய"
            if fixed[-1][1] == "o" and t[1] == "i":
                fixed[-1] = ("V", "oo~", "ஓ", "ோ")
            fixed.append(("C", "~", [g]))
        if fixed and fixed[-1][0] == "V" and fixed[-1][1] == "ei" and t[0] == "C" and t[1] == "y":
            continue
        fixed.append(t)
    toks = fixed
    n = len(toks)
    vidx = [i for i, t in enumerate(toks) if t[0] == "V"]
    nv, first_v = len(vidx), (vidx[0] if vidx else -1)
    long_final = None  # a short final vowel that is really long: questions (-uma, -udha, -iya, -ngala), -laam, -aana
    if nv >= 2 and n >= 3 and toks[-1][0] == "V" and toks[-1][1] == "a":
        c, v = toks[-2], toks[-3]
        if (c[1] == "m" and v[0] == "V" and v[1] == "u") or (c[1] in ("th", "dh") and v[0] == "V" and v[1] == "u") \
                or (c[1] == "y" and v[0] == "V" and v[1] in ("i", "ai") and nv >= 3) \
                or (c[1] == "l" and n >= 4 and toks[-4][0] == "C" and toks[-4][1] == "ng" and nv >= 4):
            long_final = n - 1
    if nv >= 3 and n >= 4 and toks[-1][1] == "a" and toks[-2][1] == "n" and toks[-3][1] == "a" and toks[-4][1] == "y":
        long_final = n - 3
    if nv >= 3 and n >= 3 and toks[-1][0] == "C" and toks[-1][1] == "m" and toks[-2][0] == "V" and toks[-2][1] == "a" \
            and toks[-3][0] == "C" and toks[-3][1] == "l":
        long_final = n - 2
    res = []
    for k, t in enumerate(toks):
        prev = toks[k - 1] if k else None
        nxt = toks[k + 1] if k + 1 < n else None
        if t[0] == "X":
            res.append(t[1])
            continue
        if t[0] == "V":
            _, lat, ind, sign = t
            if ind in LONG:
                last = k == n - 1
                before_final = k == n - 2 and nxt is not None and nxt[0] == "C" and nxt[1] in ("n", "m")
                if (ind == "எ" and (last or (before_final and nxt[1] == "n"))) or (ind == "ஒ" and (last or before_final)):
                    ind, sign = LONG[ind]
            if ind in LONG and k == first_v and prev is not None and prev[0] == "C" and nxt is not None \
                    and nxt[0] == "C" and len(nxt[2]) == 1 and k + 2 < n and toks[k + 2][0] == "V" \
                    and not (nxt[1] in ("r", "l") and toks[k + 2][1] == "i") and nxt[1] != "~":
                ind, sign = LONG[ind]  # open first syllable: pesu, podu, venum, kelu
            if k == long_final:
                ind, sign = "ஆ", "ா"
            res.append(sign if prev is not None and prev[0] == "C" else ind)
            continue
        _, lat, cs = t
        cs = list(cs)
        between = prev is not None and prev[0] == "V" and prev[1] != "ei" and nxt is not None and nxt[0] == "V"
        if lat == "n" and prev is None:
            cs = ["ந"]
        if lat == "nn" and prev is not None and prev[0] == "V" and prev[1] == "a":
            cs = ["ண", "ண"]  # pannu, kanna, thanni, anna
        if lat == "nn" and (nxt is None or nxt[0] == "C"):
            cs = [cs[0]]
        if between and lat in ("k", "p", "t"):
            cs = cs + cs  # iruku -> இருக்கு: a single k/p/t between vowels is written for a double one
        if between and lat == "ch":
            cs = ["ச", "ச"]  # aachu -> ஆச்சு
        if between and lat == "th":
            final_uthu = prev[1] == "u" and nxt[1] == "u" and k + 2 >= n - 1          # aaguthu -> ஆகுது
            short_pronoun = nv == 2 and k == 1 and prev[1] in ("a", "i", "e")          # athu, ithu, ethu
            neg_aathu = prev[1] == "aa" and k + 2 >= n and nv >= 3                    # theriyaathu -> தெரியாது
            if not (final_uthu or short_pronoun or neg_aathu):
                cs = ["த", "த"]  # eduthu -> எடுத்து
        if lat == "r" and prev is not None and prev[0] == "C" and prev[1] != "~":
            cs = ["ற"]  # solren -> சொல்றேன்
        if lat == "r" and prev is not None and prev[0] == "V" and k - 1 != first_v and nxt is not None and nxt[0] == "V":
            if "".join(x[1] for x in toks[k + 1:]) in PRESENT_TENSE_TAILS:
                cs = ["ற"]  # pannuren -> பண்ணுறேன்
        if lat == "l" and prev is not None and prev[0] == "C" and prev[1] == "v":
            cs = ["ள"]  # evlo -> எவ்ளோ
        if lat == "l" and prev is not None and prev[0] == "V" and k >= 2 and toks[k - 2][0] == "C" and toks[k - 2][1] == "ng":
            cs = ["ள"]  # irukeengala -> ...ங்களா
        if lat == "s" and nxt is not None and nxt[0] == "C":
            cs = ["ஸ"]  # jaasthi -> ஜாஸ்தி
        if lat == "h" and (nxt is None or nxt[0] != "V"):
            continue
        if lat == "ng" and nxt is None:
            cs = ["ங"]
        s = "".join(c + PULLI for c in cs[:-1]) + cs[-1]
        if nxt is None or nxt[0] != "V":
            s += PULLI
        res.append(s)
    return "".join(res)


# ---------------- which words are Tamil ----------------
_ENGLISH_MARKS = re.compile(r"(ck|tion|ght|ph[^a]|wh|qu|[xqf]|c(?!h)|ea|ie|io|ou|ue|oe|eo|y$)")
_TWO_CONS_START = re.compile(r"^(?!(th|dh|zh|sh|ch|ny|gn|kh|bh|gh|ph))[bcdfghjklmnpqrstvwxyz]{2}")
_TAMIL_ENDINGS = re.compile(r"([aeiou]|[nmlr]|zh)$")


def tamil_like(word: str) -> bool:
    """Could this unknown romanised word be Tamil? (Tamil words start with one consonant, end in a vowel or
    n/m/l/r/zh, and never contain English spellings like ck, tion, x, q, f, ea, ou.)"""
    w = word.lower()
    if len(w) < 3 or not w.isalpha() or not w.isascii():
        return False
    if _ENGLISH_MARKS.search(w) or _TWO_CONS_START.search(w) or not _TAMIL_ENDINGS.search(w):
        return False
    return True


def tamil_for(word: str, learn: bool = True) -> str | None:
    """Tamil script for one word, or None when it should stay as written (English, a name, an acronym)."""
    if not word or not word.isascii():
        return None
    low = word.lower()
    if not low.isalpha():
        return None
    d = tamil_words()
    if low in d:
        return d[low]
    if (word.isupper() and len(word) > 1) or low in english_words() or not tamil_like(low):
        return None
    if learn:
        _learn_later(low)
    return rules(low)


_WORD = re.compile(r"[A-Za-z]+")


def to_tamil(text: str, learn: bool = True) -> str:
    """Thanglish -> what the Tamil voice should read: Tamil words in Tamil script, the rest untouched."""
    if not text or not _WORD.search(text):
        return text

    def sub(m: re.Match) -> str:
        t = tamil_for(m.group(0), learn=learn)
        return m.group(0) if t is None else t
    return _WORD.sub(sub, text)


def has_tamil(text: str) -> bool:
    return any("஀" <= ch <= "௿" for ch in text)


# ---------------- background learning (Google Input Tools) ----------------
_pending: set[str] = set()
_learn_lock = threading.Lock()


def _distance(a: str, b: str) -> int:
    import difflib
    m = difflib.SequenceMatcher(None, a, b)
    return len(a) + len(b) - 2 * sum(x.size for x in m.get_matching_blocks())


def pick(candidates: list[str], guess: str) -> str:
    """Google's first suggestion, unless another of the top three is much closer to the rule-based guess (its first
    pick is sometimes a name or a formal word: 'pannu' -> 'பானு' instead of 'பண்ணு')."""
    top = candidates[0]
    best = min(candidates[:3], key=lambda c: (_distance(c, guess), candidates.index(c)))
    return best if _distance(top, guess) - _distance(best, guess) >= 3 else top


def lookup_online(word: str, timeout: float = 3.0) -> list[str]:
    import ssl
    import urllib.parse
    import urllib.request
    url = "https://inputtools.google.com/request?" + urllib.parse.urlencode(
        {"text": word, "itc": "ta-t-i0-und", "num": 3, "cp": 0, "cs": 1, "ie": "utf-8", "oe": "utf-8"})
    ctx = None
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data and data[0] == "SUCCESS" and data[1] and data[1][0][1]:
        return [c for c in data[1][0][1] if has_tamil(c)]
    return []


def _learn_later(word: str) -> None:
    if os.environ.get("DD_TRANSLIT_LEARN", "1") == "0":
        return
    with _learn_lock:
        if word in _pending or len(_pending) > 50:
            return
        _pending.add(word)
    threading.Thread(target=_learn, args=(word,), daemon=True, name="dd-thanglish-learn").start()


def _learn(word: str) -> None:
    try:
        cands = lookup_online(word)
        if not cands:
            return
        chosen = pick(cands, rules(word))
        f = learned_file()
        with _learn_lock:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data[word] = chosen
            if len(data) > 5000:
                data = dict(list(data.items())[-5000:])
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
            os.replace(tmp, f)
            if _dict is not None:
                _dict.setdefault(word, chosen)
    except Exception:
        pass
    finally:
        with _learn_lock:
            _pending.discard(word)


# ---------------- selftest ----------------
def selftest() -> int:
    import tempfile
    os.environ["DD_HOME"] = tempfile.mkdtemp(prefix="dd-thanglish-test-")
    os.environ["DD_TRANSLIT_LEARN"] = "0"
    d = tamil_words()
    assert len(d) > 1000 and d["pannu"] == "பண்ணு" and d["seri"] == "சரி" and d["aama"] == "ஆமா", len(d)
    assert len(english_words()) > 40000 and "chrome" in english_words() and "pannu" not in english_words()
    # whole sentences: Tamil words converted, English kept, punctuation and numbers untouched
    assert to_tamil("Chrome open pannitten.") == "Chrome open பண்ணிட்டேன்.", to_tamil("Chrome open pannitten.")
    s = to_tamil("Vanakkam Dhina! Innaiku weather nalla irukku, 31°C.")
    assert s == "வணக்கம் தினா! இன்னைக்கு weather நல்ல இருக்கு, 31°C.", s
    assert to_tamil("Open the YouTube app, please.") == "Open the YouTube app, please."
    assert to_tamil("CPU 40% la odudhu") == "CPU 40% ல ஓடுது", to_tamil("CPU 40% la odudhu")
    assert to_tamil("") == "" and to_tamil("123") == "123" and to_tamil("ஏற்கனவே தமிழ்") == "ஏற்கனவே தமிழ்"
    # rules on words that are not in the list
    for word, want in (("pannuvaanga", "பண்ணுவாங்க"), ("pesuren", "பேசுறேன்"), ("konjam", "கொஞ்சம்"),
                       ("sollitten", "சொல்லிட்டேன்"), ("irukku", "இருக்கு"), ("vanthuchu", "வந்துச்சு"),
                       ("thoongiten", "தூங்கிட்டேன்"), ("seithi", "செய்தி"), ("evlo", "எவ்ளோ"), ("ippo", "இப்போ")):
        assert rules(word) == want, (word, rules(word), want)
    # unknown words: Tamil-looking ones get rules, English-looking ones stay
    assert tamil_like("kaathirukken") and not tamil_like("codeigniter") and not tamil_like("nextjs") \
        and not tamil_like("stripe") and not tamil_like("happy")
    assert tamil_for("kaathirukken", learn=False) == rules("kaathirukken")
    assert tamil_for("Kubernetes", learn=False) is None and tamil_for("API", learn=False) is None
    # Google's odd first picks lose to a close rule-based spelling; ordinary ones win
    assert pick(["பானு", "பண்ணு", "பன்னு"], rules("pannu")) == "பண்ணு"
    assert pick(["அம்மா", "அம்ம", "அம்மை"], rules("amma")) == "அம்மா"
    assert pick(["கேட்டேன்", "கெட்டேன்"], rules("ketten")) == "கேட்டேன்"
    print(f"dd_thanglish selftest ok ({len(d)} Tamil words, {len(english_words())} English words, rules, pick)")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    text = " ".join(a for a in sys.argv[1:] if not a.startswith("--")) or sys.stdin.read()
    print(to_tamil(text, learn=False))
