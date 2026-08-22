"""Three-tier rendering: markdown, web HTML, email HTML."""

from datetime import date

from zotero_arxiv_daily.extract import FieldSpec
from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.report import (
    Digest,
    build_digest,
    render_email_html,
    render_markdown,
    render_web_html,
)

FIELDS = [
    FieldSpec(key="background", label="背景", instruction="i"),
    FieldSpec(key="insight", label="洞见", instruction="i"),
]


def make_paper(title, cluster, score, **kw) -> Paper:
    base = dict(
        source="pubmed",
        title=title,
        authors=["Smith J"],
        abstract="abs",
        url="https://example.org/" + title,
        score=score,
        cluster=cluster,
        journal="J Chromatogr A",
        pub_date=date(2026, 8, 18),
        doi="10.1000/" + title,
        oa_status="open",
        extraction={"background": f"{title} 背景", "insight": f"{title} 洞见"},
    )
    base.update(kw)
    return Paper(**base)


def sample_digest():
    papers = [
        make_paper("alpha", "电荷异质性", 9.0),
        make_paper("beta", "电荷异质性", 8.0),
        make_paper("gamma", "宿主细胞蛋白", 7.0, oa_status="closed"),
    ]
    backfill = [make_paper("classic", "电荷异质性", 6.0, is_backfill=True, cited_by_count=900)]
    return build_digest(papers, backfill, date(2026, 8, 21), top_n=2)


def test_digest_carries_the_week_label_and_window():
    digest = sample_digest()
    assert digest.label == "2026-08-W3"
    assert digest.start == date(2026, 8, 14)  # windows overlap by a day
    assert digest.end == date(2026, 8, 21)


def test_digest_groups_papers_by_cluster():
    digest = sample_digest()
    assert [name for name, _ in digest.clusters] == ["电荷异质性", "宿主细胞蛋白"]
    assert [p.title for p in digest.clusters[0][1]] == ["alpha", "beta"]


def test_digest_top_picks_are_the_highest_scoring_new_papers():
    assert [p.title for p in sample_digest().top_picks] == ["alpha", "beta"]


def test_backfill_is_kept_out_of_the_cluster_sections():
    digest = sample_digest()
    clustered = [p.title for _, papers in digest.clusters for p in papers]
    assert "classic" not in clustered
    assert [p.title for p in digest.backfill] == ["classic"]


def test_the_total_counts_both_new_and_backfilled_papers():
    assert sample_digest().total == 4


def test_closed_access_papers_are_listed_for_manual_retrieval():
    assert [p.title for p in sample_digest().needs_manual] == ["gamma"]


def test_markdown_contains_the_label_window_and_every_paper():
    text = render_markdown(sample_digest(), FIELDS)
    assert "2026-08-W3" in text
    assert "2026-08-14" in text and "2026-08-21" in text
    for title in ("alpha", "beta", "gamma", "classic"):
        assert title in text


def test_markdown_renders_each_configured_field_label():
    text = render_markdown(sample_digest(), FIELDS)
    assert "背景" in text
    assert "洞见" in text
    assert "alpha 洞见" in text


def test_markdown_links_dois_not_repository_paths():
    text = render_markdown(sample_digest(), FIELDS)
    assert "https://doi.org/10.1000/alpha" in text
    assert "library/" not in text


def test_markdown_labels_the_backfill_section():
    assert "经典补位" in render_markdown(sample_digest(), FIELDS)


def test_web_html_is_a_standalone_document():
    html = render_web_html(sample_digest(), FIELDS)
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in html.rstrip()
    assert "prefers-color-scheme" in html


def test_web_html_escapes_markup_in_paper_content():
    papers = [make_paper("x<script>alert(1)</script>", "c", 1.0)]
    html = render_web_html(build_digest(papers, [], date(2026, 8, 21), top_n=1), FIELDS)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_email_html_uses_table_layout_only():
    html = render_email_html(sample_digest(), FIELDS)
    assert "<table" in html
    assert "display:flex" not in html.replace(" ", "")
    assert "display:grid" not in html.replace(" ", "")
    assert "var(--" not in html
    assert "@font-face" not in html


def test_email_html_leads_with_the_top_picks():
    html = render_email_html(sample_digest(), FIELDS)
    assert html.index("alpha") < html.index("gamma")
    assert "优先" in html


def test_email_html_escapes_markup_in_paper_content():
    papers = [make_paper("x<script>alert(1)</script>", "c", 1.0)]
    html = render_email_html(build_digest(papers, [], date(2026, 8, 21), top_n=1), FIELDS)
    assert "<script>alert(1)</script>" not in html


def test_email_html_stays_under_the_gmail_clip_threshold():
    papers = [make_paper(f"paper{i}", "c", float(i)) for i in range(200)]
    html = render_email_html(build_digest(papers, [], date(2026, 8, 21), top_n=3), FIELDS)
    assert len(html.encode("utf-8")) <= 102_000


def test_email_html_says_so_when_it_had_to_truncate():
    papers = [make_paper(f"paper{i}", "c", float(i)) for i in range(200)]
    html = render_email_html(build_digest(papers, [], date(2026, 8, 21), top_n=3), FIELDS, max_bytes=4000)
    assert "完整周报见附件" in html
    assert len(html.encode("utf-8")) <= 4000


def test_an_empty_digest_still_renders():
    digest = build_digest([], [], date(2026, 8, 21), top_n=3)
    assert "2026-08-W3" in render_markdown(digest, FIELDS)
    assert "2026-08-W3" in render_email_html(digest, FIELDS)
    assert "2026-08-W3" in render_web_html(digest, FIELDS)


def test_paywalled_backfill_is_listed_for_manual_retrieval():
    """A thin week is mostly backfill: that is when the list matters most."""
    classic = make_paper("classic", "c", 5.0, is_backfill=True, oa_status="closed", cited_by_count=900)
    digest = build_digest([], [classic], date(2026, 8, 21), top_n=3)
    assert [p.title for p in digest.needs_manual] == ["classic"]
    assert "需人工取全文" in render_markdown(digest, FIELDS)


def test_email_trimming_never_keeps_a_heading_without_its_papers():
    papers = [make_paper(f"paper{i}", f"cluster{i % 12}", float(i)) for i in range(240)]
    digest = build_digest(papers, [], date(2026, 8, 21), top_n=3)
    html = render_email_html(digest, FIELDS, max_bytes=9000)
    assert len(html.encode("utf-8")) <= 9000
    for name, _ in digest.clusters:
        if f"{name}（" in html:
            heading_at = html.index(f"{name}（")
            assert "<table" in html[heading_at:], f"{name} heading kept without its list"


from zotero_arxiv_daily.extract import ListItem
from zotero_arxiv_daily.scoring import ScoreBreakdown
from zotero_arxiv_daily.triage import TriageResult

LIST_SPECS = [
    FieldSpec(key="background", label="背景", instruction="i"),
    FieldSpec(key="method", label="方法", instruction="i", kind="list"),
]


def judged_paper(**kw) -> Paper:
    paper = Paper(
        source="pubmed",
        title="ADC 表征",
        authors=["Doe Jane"],
        abstract="a",
        url="https://example.org/1",
        doi="10.1000/adc",
        journal="Separations",
        score=1.0,
        cluster="色谱",
    )
    paper.triage = TriageResult(relevance=82, reason="首次把 iCIEF 用于 AAV 衣壳", modalities=["ADC"])
    paper.scoring = ScoreBreakdown(relevance=82, journal_hit="Separations", industry_hit="Amgen", rank_score=100)
    paper.extraction = {
        "background": "抗体电荷异质性长期靠 IEX 分析",
        "method": [ListItem(point="柱系统", detail="C8 反相柱"), ListItem(point="", detail="二极管阵列检测")],
    }
    for key, value in kw.items():
        setattr(paper, key, value)
    return paper


def one_paper_digest(paper: Paper) -> Digest:
    return build_digest([paper], [], date(2026, 8, 21), top_n=1)


def test_markdown_numbers_a_list_field():
    md = render_markdown(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "1. **柱系统** — C8 反相柱" in md


def test_markdown_omits_the_dash_when_there_is_no_keyword():
    md = render_markdown(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "2. 二极管阵列检测" in md


def test_markdown_still_renders_a_text_field_inline():
    md = render_markdown(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "**背景：** 抗体电荷异质性长期靠 IEX 分析" in md


def test_markdown_shows_relevance_and_both_badges():
    md = render_markdown(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "相关度 82" in md
    assert "核心期刊" in md
    assert "企业研究（Amgen）" in md


def test_markdown_shows_the_recommendation_reason():
    md = render_markdown(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "**推荐理由：** 首次把 iCIEF 用于 AAV 衣壳" in md


def test_a_paper_without_a_journal_hit_shows_no_journal_badge():
    paper = judged_paper()
    paper.scoring = ScoreBreakdown(relevance=70, journal_hit=None, industry_hit=None, rank_score=70)
    md = render_markdown(one_paper_digest(paper), LIST_SPECS)
    assert "相关度 70" in md
    assert "核心期刊" not in md
    assert "企业研究" not in md


def test_an_unjudged_paper_renders_without_a_badge_line():
    paper = judged_paper()
    paper.triage = None
    paper.scoring = None
    md = render_markdown(one_paper_digest(paper), LIST_SPECS)
    assert "相关度" not in md
    assert "推荐理由" not in md
    assert "抗体电荷异质性长期靠 IEX 分析" in md


def test_web_html_renders_a_list_field_as_an_ordered_list():
    html = render_web_html(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "<ol" in html
    assert "<strong>柱系统</strong>" in html
    assert "C8 反相柱" in html


def test_web_html_escapes_list_item_text():
    paper = judged_paper()
    paper.extraction = {"background": "", "method": [ListItem(point="<b>x</b>", detail="a & b")]}
    html = render_web_html(one_paper_digest(paper), LIST_SPECS)
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert "a &amp; b" in html


def test_web_html_shows_the_badges():
    html = render_web_html(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "相关度 82" in html
    assert "企业研究（Amgen）" in html


def test_email_uses_the_recommendation_reason_as_the_teaser():
    # The old teaser took the first non-empty field and escaped it, which
    # raises TypeError now that a field can be a list.
    html = render_email_html(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "首次把 iCIEF 用于 AAV 衣壳" in html


def test_email_renders_without_a_triage_verdict():
    paper = judged_paper()
    paper.triage = None
    paper.scoring = None
    html = render_email_html(one_paper_digest(paper), LIST_SPECS)
    assert "ADC 表征" in html


def test_email_shows_relevance_in_the_cluster_rows():
    html = render_email_html(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "相关度 82" in html


def test_email_still_fits_its_byte_budget():
    papers = [judged_paper() for _ in range(200)]
    digest = build_digest(papers, [], date(2026, 8, 21), top_n=3)
    html = render_email_html(digest, LIST_SPECS, max_bytes=20000)
    assert len(html.encode("utf-8")) <= 20000


def test_within_cluster_order_uses_the_composite_score_not_raw_similarity():
    """Regression: bucket.sort() used raw embedding similarity, so a paper
    the LLM scored much higher on relevance could still display below a
    weaker paper that merely embedded closer to the corpus."""
    high_rank = make_paper(
        "high-rank", "membrane", 1.0,
        scoring=ScoreBreakdown(relevance=98, journal_hit=None, industry_hit=None, rank_score=98),
    )
    high_similarity = make_paper(
        "high-similarity", "membrane", 99.0,
        scoring=ScoreBreakdown(relevance=56, journal_hit=None, industry_hit=None, rank_score=56),
    )
    digest = build_digest([high_similarity, high_rank], [], date(2026, 8, 21), top_n=2)
    assert [p.title for p in digest.clusters[0][1]] == ["high-rank", "high-similarity"]
    assert [p.title for p in digest.top_picks] == ["high-rank", "high-similarity"]


def test_cluster_display_order_uses_the_composite_score_not_raw_similarity():
    """Regression: the cluster ordering itself (which section appears first)
    also used raw embedding similarity instead of the composite score."""
    high_rank = make_paper(
        "high-rank", "zzz-cluster", 1.0,
        scoring=ScoreBreakdown(relevance=98, journal_hit=None, industry_hit=None, rank_score=98),
    )
    high_similarity = make_paper(
        "high-similarity", "aaa-cluster", 99.0,
        scoring=ScoreBreakdown(relevance=56, journal_hit=None, industry_hit=None, rank_score=56),
    )
    digest = build_digest([high_similarity, high_rank], [], date(2026, 8, 21), top_n=2)
    assert [name for name, _ in digest.clusters] == ["zzz-cluster", "aaa-cluster"]
