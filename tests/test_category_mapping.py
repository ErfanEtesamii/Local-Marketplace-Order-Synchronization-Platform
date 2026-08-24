from src.didar.category_mapping import keyword_category_title


def test_matches_khatam():
    assert keyword_category_title("گلدان خاتم کاری دست ساز") == "خاتم"


def test_matches_khatam_with_zwnj():
    assert keyword_category_title("جعبه جواهرات خاتم\u200cکاری شده") == "خاتم"


def test_matches_mina_arabic_yeh_variant():
    # Arabic yeh (ي) vs Persian yeh (ی) must normalize the same way.
    assert keyword_category_title("بشقاب م\u064aناکاری اصفهان") == "مینا"


def test_technique_keyword_wins_over_generic_object_keyword():
    # "قلم‌زنی" (technique) should win over "مسی" (material/object) per
    # KEYWORD_RULES order.
    assert keyword_category_title("گلدان مسی قلمزنی شده") == "قلم\u200cزنی"


def test_khatam_wins_over_takhte_when_both_present():
    assert keyword_category_title("تخته نرد چوبی خاتم") == "خاتم"


def test_no_match_returns_none():
    assert keyword_category_title("یک محصول کاملا نامرتبط بدون کلیدواژه") is None


def test_empty_title_returns_none():
    assert keyword_category_title("") is None
    assert keyword_category_title(None) is None
