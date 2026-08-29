from src.finglish import is_latin_name, persianize_name, transliterate_finglish


def test_is_latin_name_true_for_english_letters():
    assert is_latin_name("mohammad ahmadi") is True
    assert is_latin_name("Ali") is True


def test_is_latin_name_false_for_persian_or_empty():
    assert is_latin_name("علی رضایی") is False
    assert is_latin_name("") is False
    assert is_latin_name(None) is False


def test_persianize_name_leaves_persian_untouched():
    assert persianize_name("علی رضایی") == "علی رضایی"


def test_persianize_name_leaves_none_untouched():
    assert persianize_name(None) is None


def test_persianize_name_converts_common_names_via_lookup_table():
    assert persianize_name("mohammad ahmadi") == "محمد احمدی"
    assert persianize_name("Ali Rezaei") == "علی رضایی"
    assert persianize_name("SARA KARIMI") == "سارا کریمی"


def test_persianize_name_falls_back_to_phonetic_algorithm_for_unknown_names():
    # Not in the lookup table - result just needs to be Persian script,
    # not an exact "correct" spelling (see module docstring caveats).
    result = persianize_name("Xerxes Bond")
    assert is_latin_name(result) is False
    assert result


def test_transliterate_finglish_collapses_doubled_consonants():
    # "mohammad" (doubled m) should route through the lookup table and
    # not produce a doubled Persian letter.
    assert "مم" not in transliterate_finglish("mohammad")
