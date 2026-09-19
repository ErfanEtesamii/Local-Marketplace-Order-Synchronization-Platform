from openpyxl import Workbook

from src.didar.product_catalog import ProductCatalog, _tokenize

_HEADER = [
    "_type", "عنوان محصول", "دسته بندی محصول", "کد دیدار محصول", "کد محصول",
]


def _make_catalog(tmp_path, rows):
    """Build a tiny .xlsx fixture with the same header shape as the
    client's real export, containing only the given (title, code) rows,
    and return a ProductCatalog loaded from it."""
    wb = Workbook()
    ws = wb.active
    ws.append(_HEADER)
    for title, code in rows:
        ws.append(["Product", title, None, 0, code])
    path = tmp_path / "catalog.xlsx"
    wb.save(path)
    return ProductCatalog(path)


def test_short_specific_entry_wins_over_longer_superset_entry(tmp_path):
    """Regression test for the real client example: a Digikala title
    like 'ست هدیه مسی فراز هنر مدل راستین کد 1 | چند رنگ | گارانتی
    اصالت و سلامت فیزیکی کالا' must resolve to the short catalog entry
    'راستین 1', NOT the longer 'پک هدیه راستین 1' - even though the
    longer one shares MORE words in total ("هدیه" + "راستین" + "1" vs
    just "راستین" + "1") - because "پک" never appears in the
    marketplace title (which says "ست", not "پک"), so it fails full
    containment and must be excluded outright.
    """
    catalog = _make_catalog(tmp_path, [
        ("راستین 1", "146"),
        ("پک هدیه راستین 1", "28200"),
        ("راستین 16", "161"),
    ])
    match = catalog.match(
        "ست هدیه مسی فراز هنر مدل راستین کد 1 | چند رنگ | "
        "گارانتی اصالت و سلامت فیزیکی کالا"
    )
    assert match is not None
    assert match.code == "146"
    assert match.title == "راستین 1"


def test_trailing_number_prevents_wrong_variant_match(tmp_path):
    """'راستین 16' must not match a title that only mentions '1', not
    '16' - "1" and "16" are different tokens, not a substring hit."""
    catalog = _make_catalog(tmp_path, [
        ("راستین 1", "146"),
        ("راستین 16", "161"),
    ])
    match = catalog.match("ست هدیه راستین کد 1 چند رنگ")
    assert match is not None
    assert match.code == "146"


def test_no_match_returns_none(tmp_path):
    catalog = _make_catalog(tmp_path, [("راستین 1", "146")])
    assert catalog.match("یک محصول کاملا نامرتبط بدون کلیدواژه") is None


def test_empty_title_returns_none(tmp_path):
    catalog = _make_catalog(tmp_path, [("راستین 1", "146")])
    assert catalog.match("") is None
    assert catalog.match(None) is None


def test_arabic_yeh_normalization_still_matches(tmp_path):
    # Arabic yeh (ي) in the catalog title vs Persian yeh (ی) in the
    # marketplace title must still match - same normalization as
    # category_mapping.py.
    catalog = _make_catalog(tmp_path, [("را\u064aعلي 1", "9")])
    match = catalog.match("محصول رایعلی 1 با گارانتی")
    assert match is not None
    assert match.code == "9"


def test_persian_digits_in_catalog_match_ascii_digits_in_title(tmp_path):
    """Regression test: the real client catalog mixes digit systems
    (e.g. 'قاب خاتم \u06f0\u06f9\u064817\u06f5\u064a' style rows written
    with Persian digits) while marketplace titles use ASCII digits. The
    variant number is exactly what distinguishes catalog entries, so a
    digit-system mismatch must not block an otherwise-exact match."""
    catalog = _make_catalog(tmp_path, [("قاب خاتم \u06f1\u06f0\u00d7\u06f1\u06f5", "500")])
    match = catalog.match("قاب خاتم 10x15 نیم توره پلاک نقشه ایران")
    assert match is None  # "x" (ascii letter) is not a recognized separator - by design
    match2 = catalog.match("قاب خاتم 10×15 نیم توره پلاک نقشه ایران")
    assert match2 is not None
    assert match2.code == "500"


def test_float_code_normalized_to_plain_integer_string(tmp_path):
    """Defensive: if a future Excel re-export stores Code as a real
    number (146.0) instead of text ("146"), it must still resolve to
    the plain "146" Didar actually uses as its Code - not "146.0"."""
    wb = Workbook()
    ws = wb.active
    ws.append(_HEADER)
    ws.append(["Product", "راستین 1", None, 0, 146.0])
    path = tmp_path / "catalog.xlsx"
    wb.save(path)
    catalog = ProductCatalog(path)
    match = catalog.match("ست هدیه راستین کد 1 چند رنگ")
    assert match is not None
    assert match.code == "146"


def test_glued_persian_word_and_digit_matches_separated_marketplace_title(tmp_path):
    """Regression test for a real production issue (client feedback,
    2026-09 - "product names don't match our real catalog"): the
    client's actual catalog export has rows like "چاپا4" (model name
    glued directly to its number, no space), while marketplace titles
    for the exact same product always write it as separate words plus
    a leading zero, e.g. "... مدل چاپا کد 04 ...". Confirmed against
    the real catalog export: ~128 of its ~3,300 rows have this glued
    letter+digit pattern, each one silently failing to match and
    causing a wrong duplicate product to be created under the raw
    marketplace title instead (see deal_client.py's fallback)."""
    catalog = _make_catalog(tmp_path, [
        ("چاپا4", "4"),
        ("چاپا1", "367"),
        ("چاپا اعلا 1", "36336300430001"),
    ])
    match = catalog.match(
        "رومیزی قلمکار مدل چاپا کد 04 | چند رنگ | "
        "گارانتی اصالت و سلامت فیزیکی کالا"
    )
    assert match is not None
    assert match.code == "4"


def test_missing_expected_columns_raises(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["_type", "عنوان اشتباه", "کد اشتباه"])
    ws.append(["Product", "چیزی", "1"])
    path = tmp_path / "bad.xlsx"
    wb.save(path)
    try:
        ProductCatalog(path)
        assert False, "expected ValueError for missing columns"
    except ValueError as exc:
        assert "عنوان محصول" in str(exc)


def test_missing_file_raises_clear_error(tmp_path):
    missing = tmp_path / "does-not-exist.xlsx"
    try:
        ProductCatalog(missing)
        assert False, "expected FileNotFoundError"
    except FileNotFoundError as exc:
        assert "DIDAR_PRODUCT_CATALOG_XLSX" in str(exc)


def test_glued_craft_suffix_matches_catalog_entry_without_it(tmp_path):
    """Regression test for the real production incident (Digikala order
    382920341): the catalog spells the craft as its own word ("مينا"),
    while the marketplace title glues the "-work" suffix directly onto
    it ("میناکاری", with no space) - these must still match, or
    containment fails outright and the caller falls back to an unsafe
    SKU-based Code (see deal_client.py's _build_deal_item /
    _is_collision_prone_sku)."""
    catalog = _make_catalog(tmp_path, [("قاب بشقاب 25 مينا", "2270003")])
    match = catalog.match("قاب بشقاب 25 میناکاری")
    assert match is not None
    assert match.code == "2270003"
    assert match.title == "قاب بشقاب 25 مينا"


def test_standalone_kari_word_is_not_split_into_empty_base_token():
    """The standalone word "کاری" ("job"/generic, unrelated to the
    craft-suffix case above) must tokenize to itself, not to an empty
    base token plus "کاری" - see _split_craft_suffix's length guard."""
    assert _tokenize("یک کاری برای انجام") == frozenset({"یک", "کاری", "برای", "انجام"})


# ---------------------------------------------------------------------------
# Family fallback (see product_catalog.py's FAMILY FALLBACK docstring section)
# ---------------------------------------------------------------------------

# The real Digikala title from the client's report: it says "حوضی" but
# not "شش" / "گوشه" / "تمام", so the intended catalog row fails
# containment and the generic "شکلات خوري خاتم" row wins by default.
_TITLE_316 = (
    "شکلات خوری خاتم کاری مدل حوضی کد 316 | "
    "گارانتی اصالت و سلامت فیزیکی کالا"
)
_GENERIC_ROW = ("شکلات خوري خاتم", "1290013")
_FAMILY_ROW = ("شکلات خوري حوضي شش گوشه تمام خاتم", "2610003")


class _LogSpy:
    """Stand-in for product_catalog's module-level `log` that records
    (LEVEL, formatted message) tuples. Used instead of caplog so these
    assertions don't depend on logger propagation / handler setup in
    src/logger.py."""

    def __init__(self):
        self.records = []

    def _record(self, level, msg, args):
        self.records.append((level, msg % args if args else msg))

    def warning(self, msg, *args, **kwargs):
        self._record("WARNING", msg, args)

    def info(self, msg, *args, **kwargs):
        self._record("INFO", msg, args)

    def error(self, msg, *args, **kwargs):
        self._record("ERROR", msg, args)

    def exception(self, msg, *args, **kwargs):
        self._record("EXCEPTION", msg, args)

    def messages(self, level):
        return [text for lvl, text in self.records if lvl == level]


def _spy_on_log(monkeypatch):
    spy = _LogSpy()
    monkeypatch.setattr("src.didar.product_catalog.log", spy)
    return spy


def test_family_fallback_picks_specific_sibling_over_generic_winner(tmp_path, monkeypatch):
    """The real client example: containment alone returns the generic
    'شکلات خوري خاتم' (1290013), but the title's 'حوضی' explains an
    extra word of the intended row, and everything that row adds beyond
    the title ('شش', 'گوشه', 'تمام') is a plain word, not a number - so
    the sibling row wins, with an auditable WARNING."""
    catalog = _make_catalog(tmp_path, [_GENERIC_ROW, _FAMILY_ROW])
    spy = _spy_on_log(monkeypatch)
    match = catalog.match(_TITLE_316)
    assert match is not None
    assert match.code == "2610003"
    assert match.title == _FAMILY_ROW[0]
    warnings = spy.messages("WARNING")
    assert len(warnings) == 1
    assert "family fallback" in warnings[0]
    assert _TITLE_316 in warnings[0]
    assert _GENERIC_ROW[0] in warnings[0]
    assert _FAMILY_ROW[0] in warnings[0]


def test_family_fallback_ignores_marketing_only_overlap(tmp_path, monkeypatch):
    """Marketing guard: 'پک هدیه راستین 1' shares only 'هدیه' with the
    title's head beyond the winner's words, and 'هدیه' is a marketing
    word - so the fallback must NOT fire and the short 'راستین 1'
    (146) stays the answer, with no fallback warning."""
    catalog = _make_catalog(tmp_path, [
        ("راستین 1", "146"),
        ("پک هدیه راستین 1", "28200"),
    ])
    spy = _spy_on_log(monkeypatch)
    match = catalog.match(
        "ست هدیه مسی فراز هنر مدل راستین کد 1 | چند رنگ | "
        "گارانتی اصالت و سلامت فیزیکی کالا"
    )
    assert match is not None
    assert match.code == "146"
    assert spy.messages("WARNING") == []


def test_family_fallback_never_guesses_a_number(tmp_path, monkeypatch):
    """Digit guard: the sibling 'شکلات خوري حوضي 24 خاتم' would be
    explained by the title's 'حوضی', but the title never states '24' -
    attaching the order to it would be guessing a size/count. The
    containment winner (1290013) must be kept."""
    catalog = _make_catalog(tmp_path, [
        _GENERIC_ROW,
        ("شکلات خوري حوضي 24 خاتم", "1"),
    ])
    spy = _spy_on_log(monkeypatch)
    match = catalog.match("شکلات خوری حوضی خاتم")
    assert match is not None
    assert match.code == "1290013"
    assert spy.messages("WARNING") == []


def test_family_fallback_prefers_fewest_unstated_words(tmp_path):
    """Two eligible siblings explain the same number of extra words
    ('حوضی'); the one leaving 3 words unstated beats the one leaving 4.
    The 4-word row is listed FIRST so this can't pass on catalog order
    alone."""
    catalog = _make_catalog(tmp_path, [
        _GENERIC_ROW,
        ("شکلات خوري حوضي شش گوشه تمام بزرگ خاتم", "2610004"),  # 4 leftover
        ("شکلات خوري حوضي شش گوشه تمام خاتم", "2610003"),  # 3 leftover
    ])
    match = catalog.match("شکلات خوری حوضی خاتم")
    assert match is not None
    assert match.code == "2610003"


def test_family_fallback_needs_a_containment_winner(tmp_path, monkeypatch):
    """Known limit: with no generic row for containment to pick, the
    fallback never runs - the sibling row fails containment on its own
    ('شش' etc. aren't in the title), so the result is still None."""
    catalog = _make_catalog(tmp_path, [_FAMILY_ROW])
    spy = _spy_on_log(monkeypatch)
    assert catalog.match(_TITLE_316) is None
    assert spy.messages("WARNING") == []


def test_family_fallback_survives_yeh_and_zwnj_variants(tmp_path):
    """The client's catalog spells 'خوري' / 'حوضي' with Arabic yeh
    (U+064A) while marketplace titles use Persian yeh (U+06CC) and
    often a ZWNJ inside compounds ('شکلات‌خوری', 'خاتم‌کاری'). Both
    the containment step and the fallback's head/leftover comparisons
    must still line up."""
    catalog = _make_catalog(tmp_path, [
        ("شکلات خور\u064a خاتم", "1290013"),
        ("شکلات خور\u064a حوض\u064a شش گوشه تمام خاتم", "2610003"),
    ])
    title = _TITLE_316.replace("شکلات خوری", "شکلات\u200cخوری").replace(
        "خاتم کاری", "خاتم\u200cکاری"
    )
    # Guard the fixture itself: it must really contain the variants.
    assert "\u200c" in title
    assert "\u06cc" in title and "\u064a" not in title
    match = catalog.match(title)
    assert match is not None
    assert match.code == "2610003"


def test_family_fallback_failure_degrades_to_containment_winner(tmp_path, monkeypatch):
    """match() must never raise: if the fallback step itself blows up,
    the containment winner is returned and the error is logged."""
    catalog = _make_catalog(tmp_path, [_GENERIC_ROW, _FAMILY_ROW])
    spy = _spy_on_log(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated fallback bug")

    monkeypatch.setattr(catalog, "_family_fallback", _boom)
    match = catalog.match(_TITLE_316)
    assert match is not None
    assert match.code == "1290013"
    assert len(spy.messages("EXCEPTION")) == 1