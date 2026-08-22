"""Journal and company name matching."""

from zotero_arxiv_daily.affiliation import (
    match_industry,
    match_journal,
    match_name,
    normalize,
)

JOURNALS = [
    "Molecular and Cellular Proteomics",
    "Biotechnology and Bioengineering",
    "Journal of Biological Chemistry",
    "mAbs",
    "Analytical Chemistry",
]


def test_normalize_lowercases_and_strips_punctuation():
    assert normalize("Molecular & cellular proteomics : MCP") == "molecular cellular proteomics mcp"


def test_normalize_drops_the_and_and():
    assert normalize("The Journal of Biological Chemistry") == "journal of biological chemistry"
    assert normalize("Biotechnology and Bioengineering") == "biotechnology bioengineering"


def test_normalize_keeps_of():
    assert normalize("Journal of Chromatography A") == "journal of chromatography a"


# The three rows of spec 6.2: each of these silently failed to match before
# `the`/`and` were dropped, and each is a journal the maintainer reads.
def test_ampersand_title_matches_the_and_form():
    assert match_journal("Molecular & cellular proteomics : MCP", JOURNALS) == (
        "Molecular and Cellular Proteomics"
    )


def test_and_in_journal_matches_and_in_entry():
    assert match_journal("Biotechnology and Bioengineering", JOURNALS) == (
        "Biotechnology and Bioengineering"
    )


def test_leading_the_does_not_block_the_match():
    assert match_journal("The Journal of biological chemistry", JOURNALS) == (
        "Journal of Biological Chemistry"
    )


def test_case_difference_does_not_block_the_match():
    assert match_journal("MAbs", JOURNALS) == "mAbs"


def test_short_entry_does_not_match_inside_a_longer_word():
    assert match_journal("Journal of Mabsorption Studies", JOURNALS) is None


def test_word_sequence_must_be_contiguous():
    assert match_journal("Journal of Pharmaceutical and Biomedical Analysis", ["Journal of Pharmaceutical Analysis"]) is None


def test_missing_journal_is_not_an_error():
    assert match_journal(None, JOURNALS) is None
    assert match_journal("", JOURNALS) is None


def test_empty_name_list_matches_nothing():
    assert match_journal("Analytical Chemistry", []) is None


def test_match_name_returns_the_list_entry_not_the_text():
    # The badge shows the curated name, not whatever the source happened to print.
    assert match_name("analytical chemistry letters", ["Analytical Chemistry"]) == "Analytical Chemistry"


def test_industry_matches_a_named_company():
    assert match_industry(["Amgen Inc., Thousand Oaks, CA"], [], ["Amgen", "Pfizer"]) == "Amgen"


def test_industry_falls_back_to_a_source_flagged_company():
    # OpenAlex says type == "company" for a firm nobody put on the list.
    assert match_industry(["Genentech"], ["Genentech"], ["Amgen"]) == "Genentech"


def test_named_company_wins_over_the_source_flag():
    assert match_industry(["Amgen", "Genentech"], ["Genentech"], ["Amgen"]) == "Amgen"


def test_academic_affiliation_is_not_industry():
    assert match_industry(["Tsinghua University"], [], ["Amgen"]) is None


def test_institutions_are_matched_one_by_one():
    # Joining them would let "Amgen Pfizer" match across a boundary that does
    # not exist in any single affiliation.
    assert match_industry(["Amgen", "Pfizer"], [], ["Amgen Pfizer"]) is None
