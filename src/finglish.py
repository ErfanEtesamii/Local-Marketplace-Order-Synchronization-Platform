"""
Finglish (Persian names typed with a Latin keyboard) -> Persian script.

WHY THIS EXISTS: customers on Faraz Honar (WooCommerce billing form),
Basalam, and SnappShop can type their name using an English keyboard
layout instead of a Persian one - the marketplace API happily returns
whatever they typed, e.g. "mohammad ahmadi" instead of "محمد احمدی".
That name flows straight into NormalizedOrder.customer_full_name and
from there into Didar's Contact.FirstName/LastName (see
src/didar/contact_client.py), so it shows up wrong in the CRM.

APPROACH (deliberately offline/rule-based, no external API - see
project decision 2026-08-29): there is no dictionary of "correct"
Persian spellings and no way to consult one at sync time, so this is
inherently approximate:

1. A small lookup table of common Iranian first/last names
   (persianize_name -> _COMMON_NAMES) is tried first, per word,
   case-insensitively. This covers the names that will show up most
   often and gets them exactly right.
2. Anything not in that table falls back to a phonetic letter-by-letter
   conversion (transliterate_finglish). This is a best-effort reading,
   not a real orthography engine: Persian spelling normally drops
   short vowels entirely (e.g. "Mohammad" is actually spelled with no
   written vowel between m-h and h-m), which a pure letter-substitution
   pass cannot know to do. Expect results for uncommon names to be
   readable but not always the spelling a native speaker would choose.

If a customer_full_name that comes back garbled/wrong is seen in
practice, the fix is almost always to add it (or its root name) to
_COMMON_NAMES below rather than trying to make the fallback algorithm
smarter - a growing lookup table is the more reliable of the two.

Names already in Persian script are left completely untouched
(is_latin_name returns False the moment it sees any Persian letter),
so this only ever touches names that look like they were typed in
English to begin with.
"""
from __future__ import annotations

import re

_PERSIAN_LETTER = re.compile(r"[\u0600-\u06FF]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")

# Common Iranian first names and family names, exact match (lowercased).
# Extend this as real customer names come through wrong - see module
# docstring. Keys must be lowercase with no spaces (single word only;
# _persianize_word looks words up one at a time).
_COMMON_NAMES: dict[str, str] = {
    # first names (male)
    "mohammad": "محمد", "muhammad": "محمد", "mohammed": "محمد",
    "ali": "علی", "hassan": "حسن", "hasan": "حسن",
    "hossein": "حسین", "hosein": "حسین", "hussein": "حسین",
    "reza": "رضا", "mehdi": "مهدی", "mahdi": "مهدی",
    "javad": "جواد", "hamid": "حمید", "saeed": "سعید", "said": "سعید",
    "amir": "امیر", "ahmad": "احمد", "mostafa": "مصطفی", "mustafa": "مصطفی",
    "ebrahim": "ابراهیم", "ibrahim": "ابراهیم", "abbas": "عباس",
    "vahid": "وحید", "farhad": "فرهاد", "arash": "آرش", "kourosh": "کوروش",
    "kaveh": "کاوه", "babak": "بابک", "omid": "امید", "davood": "داوود",
    "davoud": "داود", "sina": "سینا", "kian": "کیان", "milad": "میلاد",
    "pooya": "پویا", "puya": "پویا", "iman": "ایمان", "navid": "نوید",
    "farzad": "فرزاد", "kamran": "کامران", "peyman": "پیمان", "payam": "پیام",
    "ehsan": "احسان", "erfan": "عرفان", "hamed": "حامد", "hadi": "هادی",
    "meysam": "میثم", "maysam": "میثم", "shahram": "شهرام", "afshin": "افشین",
    # first names (female)
    "fatemeh": "فاطمه", "fateme": "فاطمه", "zahra": "زهرا",
    "maryam": "مریم", "sara": "سارا", "zara": "زارا", "mina": "مینا",
    "narges": "نرگس", "shirin": "شیرین", "leila": "لیلا", "leyla": "لیلا",
    "niloofar": "نیلوفر", "niloufar": "نیلوفر", "parisa": "پریسا",
    "elham": "الهام", "samira": "سمیرا", "nasrin": "نسرین", "roya": "رویا",
    "azadeh": "آزاده", "shadi": "شادی", "yasaman": "یاسمن", "yasamin": "یاسمین",
    "negar": "نگار", "atefeh": "عاطفه", "mahsa": "مهسا", "elnaz": "الناز",
    "sepideh": "سپیده", "shabnam": "شبنم", "tahereh": "طاهره",
    "raheleh": "راحله", "somayeh": "سمیه", "marzieh": "مرضیه",
    # family names
    "ahmadi": "احمدی", "mohammadi": "محمدی", "hosseini": "حسینی",
    "hosaini": "حسینی", "rezaei": "رضایی", "rezaie": "رضایی",
    "rezai": "رضایی", "karimi": "کریمی", "hashemi": "هاشمی",
    "sadeghi": "صادقی", "moradi": "مرادی", "jafari": "جعفری",
    "ghorbani": "قربانی", "rostami": "رستمی", "salehi": "صالحی",
    "bagheri": "باقری", "kazemi": "کاظمی", "amiri": "امیری",
    "esmaeili": "اسماعیلی", "esmaili": "اسماعیلی", "yousefi": "یوسفی",
    "yusefi": "یوسفی", "abbasi": "عباسی", "shirazi": "شیرازی",
    "tehrani": "تهرانی", "ghasemi": "قاسمی", "gholami": "غلامی",
    "mousavi": "موسوی", "hasani": "حسنی", "vaziri": "وزیری",
    "akbari": "اکبری", "nazari": "نظری", "zare": "زارع", "zarei": "زارعی",
    "alavi": "علوی", "naderi": "نادری", "sharifi": "شریفی",
    "ebrahimi": "ابراهیمی", "kiani": "کیانی", "soleimani": "سلیمانی",
    "soleymani": "سلیمانی", "javadi": "جوادی", "heidari": "حیدری",
    "heydari": "حیدری", "fallahi": "فلاحی", "shafiei": "شفیعی",
    # These families use "z"/"s" sounds that are spelled with a letter
    # (ظ/ض/ذ/ص) the phonetic fallback below has no way to guess - it
    # always maps that sound to ز/س (see _SINGLES) since Latin script
    # doesn't distinguish them. Must go in this table verbatim, same as
    # any other name the fallback gets wrong (see module docstring).
    "mozaffari": "مظفری", "mozafari": "مظفری", "muzaffari": "مظفری",
}

# Longest-match-first substitution of Latin digraphs that stand for a
# single Persian letter or a long vowel. Order matters: checked/applied
# before any single-letter mapping.
_DIGRAPHS = [
    ("kh", "خ"), ("gh", "ق"), ("ch", "چ"), ("sh", "ش"), ("zh", "ژ"),
    ("th", "ث"), ("ph", "ف"),
    ("aa", "آ"), ("ee", "ی"), ("oo", "و"),
    ("ou", "او"), ("ow", "او"), ("ei", "ی"), ("ey", "ی"), ("ay", "ای"),
]

_SINGLES = {
    "a": "ا", "b": "ب", "c": "ک", "d": "د", "e": "ه", "f": "ف",
    "g": "گ", "h": "ح", "i": "ی", "j": "ج", "k": "ک", "l": "ل",
    "m": "م", "n": "ن", "o": "و", "p": "پ", "q": "ق", "r": "ر",
    "s": "س", "t": "ت", "u": "و", "v": "و", "w": "و", "x": "کس",
    "y": "ی", "z": "ز",
}

# Collapses doubled Latin consonants (e.g. "mohammad" -> "mohamad")
# before mapping - Persian spelling doesn't double letters for
# gemination the way Latin transliterations often do. Vowels are
# excluded on purpose: "aa"/"ee"/"oo" are meaningful digraphs (long
# vowels), handled separately above, not accidental doubling.
_DOUBLED_CONSONANT = re.compile(r"([bcdfghjklmnpqrstvwxyz])\1+")


def is_latin_name(text: str | None) -> bool:
    """True if text contains at least one Latin letter and no Persian
    letters at all - i.e. it looks like it was typed in English/Finglish
    rather than Persian script. A name already in Persian (even mixed
    with Latin digits/punctuation) is left alone."""
    if not text:
        return False
    if _PERSIAN_LETTER.search(text):
        return False
    return bool(_LATIN_LETTER.search(text))


def _transliterate_word(word: str) -> str:
    if not _LATIN_LETTER.search(word):
        return word

    key = word.lower().strip(".,!?'\"")
    if key in _COMMON_NAMES:
        return _COMMON_NAMES[key]

    lowered = _DOUBLED_CONSONANT.sub(r"\1", word.lower())
    for pattern, repl in _DIGRAPHS:
        lowered = lowered.replace(pattern, repl)

    out = []
    for ch in lowered:
        if ch in _SINGLES:
            out.append(_SINGLES[ch])
        elif ch.isascii() and ch.isalpha():
            # Leftover unmapped Latin letter (shouldn't normally happen
            # given the table above) - drop rather than leak raw Latin
            # into an otherwise-Persian name.
            continue
        else:
            out.append(ch)
    return "".join(out)


def transliterate_finglish(text: str) -> str:
    """Best-effort phonetic conversion of Finglish text into Persian
    script, word by word. See module docstring for accuracy caveats."""
    return " ".join(_transliterate_word(w) for w in text.split(" "))


def persianize_name(full_name: str | None) -> str | None:
    """Convert full_name to Persian script if it looks like it was typed
    in Finglish/English; return it unchanged otherwise (including when
    it's already Persian, empty, or None). Intended to be called once,
    at the point where each marketplace adapter builds
    NormalizedOrder.customer_full_name."""
    if full_name is None:
        return None
    if not is_latin_name(full_name):
        return full_name
    return transliterate_finglish(full_name)
