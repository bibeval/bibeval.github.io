#!/usr/bin/env python3
"""Test suite for BibEval author verification logic.

Covers: normalization, token sets, overlap scoring, comparison, and
all edge cases discovered during development.

Run:  pytest test_author_matching.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
from bib_eval.author_verifier import (
    _normalize_author_name,
    _author_to_token_set,
    _token_overlap_score,
    _compare_authors,
    _check_year,
    _normalize_title,
    _title_similarity,
    _venue_similarity,
)

# ═══════════════════════════════════════════════════════════════════════════
# _normalize_author_name
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizeAuthorName:
    """Tests for _normalize_author_name."""

    def test_basic_lowercase(self):
        assert _normalize_author_name("John Smith") == "john smith"

    def test_strips_braces(self):
        assert _normalize_author_name("{John} Smith") == "john smith"
        assert _normalize_author_name("{John Smith}") == "john smith"

    def test_strips_dblp_disambiguation(self):
        assert _normalize_author_name("Fei Xia 0002") == "fei xia"
        assert _normalize_author_name("Andy Zeng 0001") == "andy zeng"
        assert _normalize_author_name("Peng Xu 0010") == "peng xu"

    def test_no_false_dblp_strip(self):
        """Don't strip numbers that aren't DBLP disambiguation suffixes."""
        assert _normalize_author_name("GPT 4") == "gpt 4"  # not 4-digit suffix
        assert _normalize_author_name("Paper 2023") == "paper 2023"  # year, not disambig

    def test_comma_lastname_firstname(self):
        """Comma-separated names: just remove comma, don't swap."""
        assert _normalize_author_name("Kranti, Chalamalasetti") == "kranti chalamalasetti"
        assert _normalize_author_name("Smith, John") == "smith john"

    def test_comma_no_space(self):
        assert _normalize_author_name("Smith,John") == "smith john"

    def test_extra_whitespace(self):
        assert _normalize_author_name("  John   Smith  ") == "john smith"

    def test_unicode_diacritics(self):
        """NFKD normalization strips diacritics."""
        assert _normalize_author_name("Oğuzhan Fatih Kar") == "oguzhan fatih kar"
        assert _normalize_author_name("Hüttenrauch") == "huttenrauch"
        assert _normalize_author_name("José") == "jose"
        assert _normalize_author_name("François") == "francois"

    def test_hyphenated_names(self):
        """Hyphens in names are preserved."""
        assert "li fei-fei" in _normalize_author_name("Li Fei-Fei")
        assert "narayan-chen" in _normalize_author_name("Anjali Narayan-Chen")

    def test_periods_in_initials(self):
        """Periods after initials become spaces, then collapsed."""
        result = _normalize_author_name("J. P. Morgan")
        assert "j" in result
        assert "p" in result
        assert "morgan" in result

    def test_et_al(self):
        """'et al' should be preserved as-is."""
        assert "et al" in _normalize_author_name("et al")
        assert "et al" in _normalize_author_name("  et al  ")


# ═══════════════════════════════════════════════════════════════════════════
# _author_to_token_set
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthorToTokenSet:
    """Tests for _author_to_token_set."""

    def test_basic(self):
        assert _author_to_token_set("John Smith") == {"john", "smith"}

    def test_single_name(self):
        assert _author_to_token_set("Madonna") == {"madonna"}

    def test_keeps_initials(self):
        """Single-char tokens (initials) are kept for initial-aware matching."""
        tokens = _author_to_token_set("F. Xia")
        assert "f" in tokens
        assert "xia" in tokens

    def test_middle_names(self):
        tokens = _author_to_token_set("Lucy Xiaoyang Shi")
        assert tokens == {"lucy", "xiaoyang", "shi"}

    def test_comma_format(self):
        """'LastName, FirstName' → same tokens as 'FirstName LastName'."""
        a = _author_to_token_set("Kranti, Chalamalasetti")
        b = _author_to_token_set("Chalamalasetti Kranti")
        assert a == b

    def test_unicode(self):
        tokens = _author_to_token_set("Oğuzhan Fatih Kar")
        assert "oguzhan" in tokens
        assert "fatih" in tokens
        assert "kar" in tokens

    def test_empty(self):
        assert _author_to_token_set("") == frozenset()
        assert _author_to_token_set("  ") == frozenset()

    def test_dblp_suffix_removed(self):
        tokens = _author_to_token_set("Fei Xia 0002")
        assert "0002" not in tokens


# ═══════════════════════════════════════════════════════════════════════════
# _token_overlap_score
# ═══════════════════════════════════════════════════════════════════════════


class TestTokenOverlapScore:
    """Tests for _token_overlap_score — the core matching algorithm."""

    def test_exact_match(self):
        score = _token_overlap_score({"john", "smith"}, {"john", "smith"})
        assert score == 1.0

    def test_no_overlap(self):
        score = _token_overlap_score({"alice", "bob"}, {"carol", "dave"})
        assert score == 0.0

    def test_initial_matches_full_name(self):
        """'F. Xia' should match 'Fei Xia'."""
        score = _token_overlap_score({"f", "xia"}, {"fei", "xia"})
        assert score >= 0.5, f"Expected >=0.5, got {score}"

    def test_full_name_matches_initial(self):
        """Reverse: 'Fei Xia' should match 'F. Xia'."""
        score = _token_overlap_score({"fei", "xia"}, {"f", "xia"})
        assert score >= 0.5, f"Expected >=0.5, got {score}"

    def test_middle_initial(self):
        """'Leonidas J. Guibas' should match 'Leonidas Guibas'."""
        score = _token_overlap_score(
            {"leonidas", "j", "guibas"},
            {"leonidas", "guibas"},
        )
        assert score >= 0.5, f"Expected >=0.5, got {score}"

    def test_middle_name_missing(self):
        """'Lucy Xiaoyang Shi' should match 'Lucy Shi'."""
        score = _token_overlap_score(
            {"lucy", "xiaoyang", "shi"},
            {"lucy", "shi"},
        )
        assert score >= 0.5, f"Expected >=0.5, got {score}"

    def test_middle_initial_z(self):
        """'Tony Z. Zhao' should match 'Tony Zhao'."""
        score = _token_overlap_score(
            {"tony", "z", "zhao"},
            {"tony", "zhao"},
        )
        assert score >= 0.5, f"Expected >=0.5, got {score}"

    def test_initial_only_both_sides(self):
        """'R. Lachmy' should match 'Royi Lachmy'."""
        score = _token_overlap_score({"r", "lachmy"}, {"royi", "lachmy"})
        assert score >= 0.5, f"Expected >=0.5, got {score}"

    def test_different_initials(self):
        """'A. Smith' should NOT match 'B. Smith'."""
        score = _token_overlap_score({"a", "smith"}, {"b", "smith"})
        assert score < 0.5, f"Expected <0.5, got {score}"

    def test_subset_single_name(self):
        """One name with extra parts should still match."""
        score = _token_overlap_score(
            {"peter", "florence"},
            {"peter", "r", "florence"},
        )
        assert score >= 0.5

    def test_empty_sets(self):
        assert _token_overlap_score(frozenset(), {"john"}) == 0.0
        assert _token_overlap_score({"john"}, frozenset()) == 0.0
        assert _token_overlap_score(frozenset(), frozenset()) == 0.0

    def test_symmetric(self):
        """Score should be symmetric."""
        a = {"f", "xia"}
        b = {"fei", "xia"}
        assert _token_overlap_score(a, b) == _token_overlap_score(b, a)


# ═══════════════════════════════════════════════════════════════════════════
# _compare_authors
# ═══════════════════════════════════════════════════════════════════════════


class TestCompareAuthors:
    """Tests for _compare_authors — full author list comparison."""

    # ── Exact matches ──

    def test_exact_match(self):
        is_match, missing, extra, order_wrong = _compare_authors(
            ["John Smith", "Alice Jones"],
            ["John Smith", "Alice Jones"],
        )
        assert is_match is True
        assert missing == []
        assert extra == []
        assert order_wrong is False

    def test_exact_match_lastname_first(self):
        """Comma format in bib should match First Last from source."""
        is_match, missing, extra, order_wrong = _compare_authors(
            ["Kranti, Chalamalasetti", "Hakimov, Sherzod", "Schlangen, David"],
            ["Chalamalasetti Kranti", "Sherzod Hakimov", "David Schlangen"],
        )
        assert is_match is True
        assert order_wrong is False

    # ── Initial-based matches ──

    def test_initials_in_source(self):
        """Source has initials, bib has full names."""
        is_match, missing, extra, order_wrong = _compare_authors(
            ["Fei Xia", "Aakanksha Chowdhery", "Quan Vuong", "Sergey Levine"],
            ["F. Xia", "A. Chowdhery", "Q. Vuong", "S. Levine"],
        )
        assert is_match is True
        assert missing == []
        assert order_wrong is False  # same order, just different name format

    def test_initials_in_bib(self):
        """Bib has initials, source has full names."""
        is_match, missing, extra, order_wrong = _compare_authors(
            ["R. Lachmy"],
            ["Royi Lachmy"],
        )
        assert is_match is True
        assert order_wrong is False  # single author, but explicit assertion

    # ── Middle names/initials ──

    def test_middle_initial_extra(self):
        """Source has middle initial, bib doesn't."""
        is_match, missing, extra, order_wrong = _compare_authors(
            ["Leonidas Guibas"],
            ["Leonidas J. Guibas"],
        )
        assert is_match is True

    def test_middle_name_missing_bib(self):
        """Bib missing middle name that source has."""
        is_match, missing, extra, order_wrong = _compare_authors(
            ["Lucy Shi", "Tony Zhao"],
            ["Lucy Xiaoyang Shi", "Tony Z. Zhao"],
        )
        assert is_match is True

    # ── Unicode ──

    def test_unicode_diacritic(self):
        """Diacritic in bib, ASCII in source — should match."""
        is_match, missing, extra, order_wrong = _compare_authors(
            ["Oğuzhan Fatih Kar"],
            ["Oguzhan Fatih Kar"],
        )
        assert is_match is True

    # ── Order checks ──

    def test_order_wrong(self):
        is_match, missing, extra, order_wrong = _compare_authors(
            ["Alice", "Bob", "Carol"],
            ["Bob", "Alice", "Carol"],
        )
        assert is_match is True  # same people, just wrong order
        assert order_wrong is True

    def test_order_correct(self):
        is_match, missing, extra, order_wrong = _compare_authors(
            ["Alice", "Bob", "Carol"],
            ["Alice", "Bob", "Carol"],
        )
        assert order_wrong is False

    def test_order_middle_swapped(self):
        """Middle authors swapped — should be caught."""
        is_match, missing, extra, order_wrong = _compare_authors(
            ["A", "B", "C", "D"],
            ["A", "C", "B", "D"],
        )
        assert is_match is True
        assert order_wrong is True

    # ── Missing/extra ──

    def test_missing_author(self):
        is_match, missing, extra, order_wrong = _compare_authors(
            ["Alice"],
            ["Alice", "Bob"],
        )
        assert is_match is False
        assert "Bob" in missing[0]
        assert extra == []

    def test_extra_author(self):
        is_match, missing, extra, order_wrong = _compare_authors(
            ["Alice", "Bob", "Carol"],
            ["Alice", "Bob"],
        )
        assert is_match is False
        assert "Carol" in extra[0]
        assert missing == []

    def test_different_author(self):
        is_match, missing, extra, order_wrong = _compare_authors(
            ["Alice"],
            ["Bob"],
        )
        assert is_match is False
        assert len(missing) == 1
        assert len(extra) == 1

    # ── DBLP disambiguation ──

    def test_dblp_suffix_not_causing_mismatch(self):
        """DBLP 'Fei Xia 0002' should match bib 'Fei Xia'."""
        is_match, missing, extra, order_wrong = _compare_authors(
            ["Jacky Liang", "Wenlong Huang", "Fei Xia", "Peng Xu",
             "Karol Hausman", "Brian Ichter", "Pete Florence", "Andy Zeng"],
            ["Jacky Liang", "Wenlong Huang", "Fei Xia 0002", "Peng Xu 0010",
             "Karol Hausman", "Brian Ichter", "Pete Florence", "Andy Zeng 0001"],
        )
        assert is_match is True
        assert missing == []
        assert order_wrong is False  # DBLP suffixes should not cause false order mismatch

    def test_dblp_suffix_order_not_flagged(self):
        """DBLP suffixes at matching positions should not cause order_wrong."""
        _, _, _, order_wrong = _compare_authors(
            ["Fei Xia", "Andy Zeng"],
            ["Fei Xia 0002", "Andy Zeng 0001"],
        )
        assert order_wrong is False

    # ── Edge: single author ──

    def test_single_author_match(self):
        is_match, _, _, order_wrong = _compare_authors(["Alice"], ["Alice"])
        assert is_match is True
        assert order_wrong is False  # single author has no order

    def test_single_author_mismatch(self):
        is_match, _, _, _ = _compare_authors(["Alice"], ["Bob"])
        assert is_match is False

    # ── Edge: empty lists ──

    def test_both_empty(self):
        is_match, missing, extra, order_wrong = _compare_authors([], [])
        assert is_match is True
        assert missing == []
        assert extra == []

    def test_empty_bib(self):
        is_match, missing, extra, _ = _compare_authors([], ["Alice"])
        assert is_match is False
        assert "Alice" in missing[0]

    def test_empty_source(self):
        is_match, missing, extra, _ = _compare_authors(["Alice"], [])
        assert is_match is False
        assert "Alice" in extra[0]

    # ── Edge: duplicate-like names ──

    def test_two_smiths_different_first_names(self):
        """John Smith and Jane Smith are different people."""
        is_match, missing, extra, _ = _compare_authors(
            ["John Smith"],
            ["Jane Smith"],
        )
        # 'john smith' vs 'jane smith' — tokens: {john, smith} vs {jane, smith}
        # Overlap: |{smith}| / |{john, smith, jane}| = 1/3 = 0.33 < 0.5
        assert is_match is False

    # ── Edge: "et al." in authors ──

    def test_et_al_in_bib(self):
        """'et al' in bib should match source prefix."""
        is_match, _, _, _ = _compare_authors(
            ["Brian Ichter", "Anthony Brohan", "Yevgen Chebotar", "Chelsea Finn", "et al"],
            ["Brian Ichter", "Anthony Brohan", "Yevgen Chebotar", "Chelsea Finn",
             "Karol Hausman", "Pete Florence", "Andy Zeng"],
        )
        assert is_match is True

    def test_et_al_with_period(self):
        """'et al.' (with period) should also be detected."""
        is_match, _, _, _ = _compare_authors(
            ["Alice", "et al."],
            ["Alice", "Bob"],
        )
        assert is_match is True

    def test_et_al_in_source(self):
        """Source has 'et al', bib has full list — only prefix compared."""
        is_match, _, _, _ = _compare_authors(
            ["Alice", "Bob", "Carol"],
            ["Alice", "et al"],
        )
        assert is_match is True

    def test_et_al_prefix_mismatch(self):
        """Prefix before 'et al' doesn't match — still a failure."""
        is_match, _, _, _ = _compare_authors(
            ["Alice", "Wrong", "et al"],
            ["Alice", "Bob", "Carol"],
        )
        assert is_match is False

    def test_et_al_both_sides(self):
        """Both sides have 'et al' — compare prefix of the shorter one."""
        is_match, _, _, _ = _compare_authors(
            ["Alice", "Bob", "et al"],
            ["Alice", "et al"],
        )
        # cutoff = min(2, 1) = 1 → compare just ["Alice"] vs ["Alice"]
        assert is_match is True

    def test_et_al_order_check(self):
        """Order of prefix authors still matters."""
        _, _, _, order_wrong = _compare_authors(
            ["Bob", "Alice", "et al"],
            ["Alice", "Bob", "Carol"],
        )
        # cutoff = min(2, 3) = 2 → compare ["Bob", "Alice"] vs ["Alice", "Bob"]
        # Authors match (same set) but order is wrong
        assert order_wrong is True

    def test_et_al_only(self):
        """Just 'et al' as the only author — trivially matches anything."""
        is_match, _, _, _ = _compare_authors(
            ["et al"],
            ["Alice", "Bob", "Carol"],
        )
        assert is_match is True  # cutoff=0, both truncated to empty → match


# ═══════════════════════════════════════════════════════════════════════════
# _venue_similarity
# ═══════════════════════════════════════════════════════════════════════════


class TestVenueSimilarity:
    """Tests for _venue_similarity."""

    def test_exact_match(self):
        assert _venue_similarity("NeurIPS", "NeurIPS") is True

    def test_one_contains_other(self):
        assert _venue_similarity(
            "Advances in Neural Information Processing Systems",
            "Neural Information Processing Systems",
        ) is True

    def test_abbreviation_fuzzy(self):
        """'NeurIPS' vs full name passes fuzzy threshold."""
        assert _venue_similarity(
            "Advances in Neural Information Processing Systems",
            "NeurIPS",
        ) is True

    def test_completely_different(self):
        assert _venue_similarity("NeurIPS", "ICML") is False

    def test_normalized_match(self):
        """Case and punctuation differences."""
        assert _venue_similarity(
            "Proceedings of NeurIPS 2022",
            "proceedings of neurips 2022",
        ) is True

    def test_empty_bib_venue(self):
        assert _venue_similarity("", "NeurIPS") is True

    def test_empty_source_venue(self):
        assert _venue_similarity("NeurIPS", "") is True

    def test_both_empty(self):
        assert _venue_similarity("", "") is True

    def test_acl_variants(self):
        assert _venue_similarity(
            "Proceedings of the 58th Annual Meeting of the ACL",
            "ACL",
        ) is True

    def test_icml_vs_neurips(self):
        """Should NOT match different conferences."""
        assert _venue_similarity("ICML", "NeurIPS") is False


# ═══════════════════════════════════════════════════════════════════════════
# _check_year
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckYear:
    """Tests for _check_year."""

    def test_exact_match(self):
        assert _check_year("2023", "2023") is True

    def test_one_year_off(self):
        """±1 year tolerance for preprint vs publication date."""
        assert _check_year("2022", "2023") is True
        assert _check_year("2024", "2023") is True

    def test_two_years_off(self):
        assert _check_year("2021", "2023") is False

    def test_empty_bib_year(self):
        assert _check_year("", "2023") is True

    def test_empty_source_year(self):
        assert _check_year("2023", "") is True

    def test_both_empty(self):
        assert _check_year("", "") is True

    def test_non_numeric(self):
        assert _check_year("abc", "2023") is True  # can't parse, assume match

    def test_year_in_string(self):
        """Crossref sometimes returns year as int."""
        assert _check_year("2023", 2023) is True


# ═══════════════════════════════════════════════════════════════════════════
# _normalize_title / _title_similarity
# ═══════════════════════════════════════════════════════════════════════════


class TestTitleNormalization:
    """Tests for _normalize_title and _title_similarity."""

    def test_basic(self):
        assert _normalize_title("Hello World") == "hello world"

    def test_removes_punctuation(self):
        assert _normalize_title("Hello, World!") == "hello world"
        assert _normalize_title("Pre-train, Prompt, and Predict") == "pre train prompt and predict"

    def test_collapse_whitespace(self):
        assert _normalize_title("Hello   World") == "hello world"

    def test_newlines_in_title(self):
        """Titles with embedded newlines should normalize."""
        t = "VoxPoser: Composable 3D Value Maps for Robotic Manipulation with Language\nModels"
        normalized = _normalize_title(t)
        assert "\n" not in normalized
        assert "models" in normalized

    def test_similarity_exact(self):
        assert _title_similarity("Hello World", "Hello World") == 1.0

    def test_similarity_close(self):
        score = _title_similarity(
            "Attention Is All You Need",
            "Attention is all you need",
        )
        assert score > 0.95

    def test_similarity_different(self):
        score = _title_similarity("Hello World", "Goodbye Mars")
        assert score < 0.5

    def test_similarity_newline(self):
        """Titles with/without newlines should be similar."""
        score = _title_similarity(
            "VoxPoser: Composable 3D Value Maps for Robotic Manipulation with Language Models",
            "VoxPoser: Composable 3D Value Maps for Robotic Manipulation with Language\nModels",
        )
        assert score > 0.95


# ═══════════════════════════════════════════════════════════════════════════
# Integration: real-world bug regression tests
# ═══════════════════════════════════════════════════════════════════════════


class TestRegression:
    """Tests based on actual bugs found during development."""

    def test_dblp_disambiguation_regression(self):
        """Bug: DBLP suffixes caused false 'missing author' flags."""
        bib = [
            "Jacky Liang", "Wenlong Huang", "Fei Xia", "Peng Xu",
            "Karol Hausman", "Brian Ichter", "Pete Florence", "Andy Zeng",
        ]
        dblp = [
            "Jacky Liang", "Wenlong Huang", "Fei Xia 0002", "Peng Xu 0010",
            "Karol Hausman", "Brian Ichter", "Pete Florence", "Andy Zeng 0001",
        ]
        is_match, missing, extra, _ = _compare_authors(bib, dblp)
        assert is_match is True
        assert missing == []
        assert extra == []

    def test_initial_fullname_regression(self):
        """Bug: 'F. Xia' vs 'Fei Xia' was flagged as mismatch."""
        bib = ["Fei Xia", "Aakanksha Chowdhery", "Quan Vuong",
               "Pierre Sermanet", "Sergey Levine", "Pete Florence"]
        src = ["F. Xia", "A. Chowdhery", "Q. Vuong",
               "P. Sermanet", "S. Levine", "Peter R. Florence"]
        is_match, _, _, _ = _compare_authors(bib, src)
        assert is_match is True

    def test_middle_initial_regression(self):
        """Bug: 'Leonidas J. Guibas' vs 'Leonidas Guibas' was flagged."""
        is_match, _, _, _ = _compare_authors(
            ["Leonidas J. Guibas"], ["Leonidas Guibas"]
        )
        assert is_match is True

    def test_unicode_regression(self):
        """Bug: 'Oğuzhan' vs 'Oguzhan' was flagged as mismatch."""
        is_match, _, _, _ = _compare_authors(
            ["Oğuzhan Fatih Kar"], ["Oguzhan Fatih Kar"]
        )
        assert is_match is True

    def test_middle_name_regression(self):
        """Bug: 'Lucy Xiaoyang Shi' vs 'Lucy Shi' was flagged."""
        is_match, _, _, _ = _compare_authors(
            ["Lucy Shi", "Tony Zhao"],
            ["Lucy Xiaoyang Shi", "Tony Z. Zhao"],
        )
        assert is_match is True

    def test_initial_only_regression(self):
        """Bug: 'R. Lachmy' vs 'Royi Lachmy' was flagged."""
        is_match, _, _, _ = _compare_authors(
            ["R. Lachmy"], ["Royi Lachmy"]
        )
        assert is_match is True

    def test_comma_format_regression(self):
        """Bug: ACL 'LastName, FirstName' format didn't match sources."""
        is_match, _, _, _ = _compare_authors(
            ["Kranti, Chalamalasetti", "Hakimov, Sherzod", "Schlangen, David"],
            ["Chalamalasetti Kranti", "Sherzod Hakimov", "David Schlangen"],
        )
        assert is_match is True

    def test_order_all_positions_regression(self):
        """Bug: only first/last were checked; middle swap was missed."""
        _, _, _, order_wrong = _compare_authors(
            ["A", "B", "C", "D"],
            ["A", "C", "B", "D"],
        )
        assert order_wrong is True

    def test_author_list_with_et_al(self):
        """Papers with 30+ authors often use 'et al' in bib files.
        Only prefix authors before 'et al' are compared — if they match,
        the entry is considered correct.
        """
        is_match, _, _, _ = _compare_authors(
            ["Tom Brown", "et al"],
            ["Tom B. Brown", "Benjamin Mann", "Nick Ryder"],
        )
        # cutoff=1 → compare ["Tom Brown"] vs ["Tom B. Brown"] → initial-aware match
        assert is_match is True

    def test_german_umlaut_unicode_vs_ascii(self):
        """Bug: 'Hüttenrauch' (ü) vs 'Huettenrauch' (ue) was flagged."""
        is_match, _, _, _ = _compare_authors(
            ["Helge Hüttenrauch"],
            ["Helge Huettenrauch"],
        )
        assert is_match is True

    def test_german_umlaut_mueller(self):
        """'Müller' vs 'Mueller' — both normalize to 'muller'."""
        assert _author_to_token_set("Müller") == _author_to_token_set("Mueller")

    def test_german_umlaut_schroeder(self):
        """'Schröder' vs 'Schroeder' — both normalize to 'schroder'."""
        assert _author_to_token_set("Schröder") == _author_to_token_set("Schroeder")

    def test_german_umlaut_does_not_overstrip(self):
        """'Bauer' should NOT normalize to 'baur' (ee case was fixed)."""
        tokens = _author_to_token_set("Bauer")
        assert "baur" not in tokens
        assert "bauer" in tokens

    def test_fuzzy_name_fallback(self):
        """Bug: 'Yucheng Suo' vs 'Yuchen Suo' fails token match but
        should pass via SequenceMatcher ≥0.85 fallback."""
        is_match, _, _, _ = _compare_authors(
            ["Yucheng Suo"], ["Yuchen Suo"],
        )
        assert is_match is True


# ═══════════════════════════════════════════════════════════════════════════
# CLI runner
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
