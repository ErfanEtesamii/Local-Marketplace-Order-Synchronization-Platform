"""
Title -> Didar product-category keyword mapping.

WHY THIS EXISTS
----------------
Only Faraz Honar (WooCommerce) sends a real category name with each order
item (see OrderItem.category / farazhonar.py's _resolve_category). The
other four sources - Digikala, Basalam, Tapsi Shop, SnappShop - do not
expose a category field at all in the schemas this project talks to
(confirmed by reading each adapter's _group_rows_into_orders /
_normalize_* method: none of them read anything category-like off the
raw API payload). For those four, the only signal available at all is
the item's own title string.

This module guesses a Didar category from that title using a simple
keyword table - NOT a general NLP classifier. It is deliberately dumb:
substring match, first rule in KEYWORD_RULES that hits wins. A silent
wrong guess is worse than falling through to the catch-all, so the
keyword lists below are meant to be conservative (craft-specific terms),
not broad (e.g. "طلا" is NOT a keyword for anything here, because gold
could plausibly appear in several categories' products).

STATUS: DRAFT, NOT CONFIRMED AGAINST THE REAL CATALOG.
These keyword lists were authored from the Didar category *names*
themselves (see DidarProductClient.list_categories() output the client
shared: خاتم, پرداز, قاب, مسی, متفرقه, هنر پارچه, تخته, آیینه, مینا/مينا,
فیروزه, ساعت, الماس تراش, قلم‌زنی, نقاشی, اکسسوری, پک) plus common
Persian spelling/phrasing variants of each craft. Nobody has yet checked
these against real Digikala/Basalam/... product titles from this
client's actual catalog. Treat every list below as a first draft:
- Missing a real product title's wording -> falls through silently to
  the DIDAR_DEFAULT_PRODUCT_CATEGORY_ID catch-all (متفرقه), not an
  error. Check the sync logs' "no keyword matched" entries (see
  product_client.py) periodically and add the missing term here.
- A keyword that's too broad -> silent WRONG category, which is worse
  than falling through. When adding a keyword, prefer the more specific
  phrasing over a bare word that could appear in an unrelated product
  title.

"مینا"/"مينا": Didar's own category list (as returned by list_categories())
contains BOTH spellings as two separate Title strings - Arabic yeh (ي,
U+064A) in one and Persian yeh (ی, U+06CC) in the other. _normalize_fa()
below folds both to the Persian form before comparing, so a title
containing either spelling resolves the same way; which of the two
actual Didar category Ids gets used depends on which one exact-matches
after normalization in product_client.py's category-by-title cache
(whichever the API happens to return - not something this module
controls). Flag to the client: consider merging/renaming the duplicate
category in Didar itself so only one Id exists going forward.

ORDER MATTERS: KEYWORD_RULES is checked top-to-bottom, first hit wins.
Titles combining two crafts (e.g. "قاب خاتم‌کاری" - a خاتم-inlaid frame)
are genuinely ambiguous; the order below puts the more specific
craft-technique terms (خاتم, میناکاری, قلم‌زنی, فیروزه‌کوبی...) ahead of
generic object-type terms (قاب, ساعت, آیینه...) on the theory that the
technique is usually the more useful classification for this catalog.
Reorder this list if the client's actual practice differs.
"""
from __future__ import annotations

# (Didar category Title exactly as returned by list_categories(), [keywords])
# Keywords are matched as case/whitespace-insensitive substrings of the
# item title, after Persian character normalization (see _normalize_fa).
KEYWORD_RULES: list[tuple[str, list[str]]] = [
    ("خاتم", ["خاتم کاری", "خاتم‌کاری", "خاتم سازی", "خاتم‌سازی", "خاتم"]),
    ("مینا", ["میناکاری", "مینا کاری", "میناسازی", "مینا سازی", "میناپزی", "مینا"]),
    ("قلم‌زنی", ["قلم زنی", "قلم‌زنی", "قلمزنی"]),
    ("فیروزه", ["فیروزه کوبی", "فیروزه‌کوبی", "فیروزه کوب", "فیروزه‌کوب", "فیروزه"]),
    ("الماس تراش", ["الماس تراشی", "الماس‌تراشی", "الماس تراش", "تراش الماس"]),
    ("نقاشی", ["نقاشی", "نگارگری", "تابلو نقاشی"]),
    ("هنر پارچه", ["سوزن دوزی", "سوزن‌دوزی", "گلدوزی", "تابلو فرش", "تابلوفرش", "پارچه"]),
    ("مسی", ["مسی", "مس کوبی", "مس‌کوبی"]),
    ("پرداز", ["پرداز"]),
    ("آیینه", ["آینه کاری", "آینه‌کاری", "آیینه کاری", "آیینه‌کاری", "آینه", "آیینه"]),
    ("تخته", ["تخته نرد", "تخته‌نرد", "تخته"]),
    ("ساعت", ["ساعت دیواری", "ساعت رومیزی", "ساعت مچی", "ساعت"]),
    ("قاب", ["قاب عکس", "قابسازی", "قاب‌سازی", "قاب"]),
    ("پک", ["پک هدیه", "باکس هدیه", "ست هدیه", "پک"]),
    ("اکسسوری", ["دستبند", "گردنبند", "انگشتر", "گوشواره", "اکسسوری"]),
]


def _normalize_fa(text: str) -> str:
    """Lowercase/whitespace-fold plus the two Persian-text quirks that
    would otherwise break a plain substring match: Arabic ي/ك vs.
    Persian ی/ک, and stray zero-width non-joiners some titles include
    around prefixes (e.g. "می‌نویسم"-style ZWNJ, U+200C)."""
    text = text.replace("\u064a", "\u06cc").replace("\u0643", "\u06a9")  # ي->ی, ك->ک
    text = text.replace("\u200c", " ")  # ZWNJ -> space, so "خاتم‌کاری" ~ "خاتم کاری"
    return " ".join(text.split()).casefold()


def keyword_category_title(item_title: str) -> str | None:
    """Best-guess Didar category Title for a marketplace item title, or
    None if nothing in KEYWORD_RULES matched. Caller (product_client.py)
    is responsible for turning the Title into an Id and for the final
    fallback to the catch-all category."""
    if not item_title:
        return None
    normalized = _normalize_fa(item_title)
    for category_title, keywords in KEYWORD_RULES:
        for kw in keywords:
            if _normalize_fa(kw) in normalized:
                return category_title
    return None
