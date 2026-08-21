"""Three-tier rendering: markdown, web HTML, email HTML."""

from datetime import date

from zotero_arxiv_daily.extract import FieldSpec
from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.report import (
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
    assert digest.start == date(2026, 8, 15)
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
    assert "2026-08-15" in text and "2026-08-21" in text
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
