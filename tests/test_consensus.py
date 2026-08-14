import pytest

from melredact.consensus import analyze_consensus_anomalies, format_consensus_report
from melredact.pipeline import packet_tag
from melredact.segment import segment_pdf
from tests.make_fixture import build_consensus_fixture, build_small_consensus_group_fixture


@pytest.fixture(scope="module")
def consensus_fixture(tmp_path_factory):
    return build_consensus_fixture(tmp_path_factory.mktemp("consensus_fixture"))


@pytest.fixture(scope="module")
def consensus_segmented(consensus_fixture):
    return segment_pdf(consensus_fixture.pdf_path)


@pytest.fixture(scope="module")
def consensus_analysis(consensus_fixture, consensus_segmented):
    return analyze_consensus_anomalies(consensus_fixture.pdf_path, consensus_segmented)


def test_anomalous_ink_on_page_2_is_flagged_with_page_index_in_reason(consensus_fixture, consensus_analysis):
    holds = consensus_analysis.holds_for(consensus_fixture.anomaly_tag)
    assert len(holds) == 1
    hold = holds[0]
    assert hold.page_offset == consensus_fixture.anomaly_page_offset
    assert "page 2" in hold.reason
    assert hold.occurrence_count == 1


def test_shared_answer_ink_at_a_position_most_of_the_group_shares_is_not_flagged(consensus_fixture, consensus_analysis):
    for tag in consensus_fixture.answer_tags:
        assert consensus_analysis.holds_for(tag) == [], f"{tag} should not be held for shared answer-field ink"


def test_clean_packets_with_no_extra_ink_are_not_flagged(consensus_fixture, consensus_analysis):
    for tag in consensus_fixture.clean_tags:
        assert consensus_analysis.holds_for(tag) == []


def test_only_the_anomalous_packet_is_held_in_this_group(consensus_fixture, consensus_analysis):
    assert set(consensus_analysis.holds) == {consensus_fixture.anomaly_tag}


def test_header_page_is_never_a_source_of_holds(consensus_fixture, consensus_analysis, consensus_segmented):
    header_offset = 0
    for holds in consensus_analysis.holds.values():
        for hold in holds:
            assert hold.page_offset != header_offset


def test_format_consensus_report_names_the_anomalous_tag(consensus_fixture, consensus_analysis):
    report = format_consensus_report(consensus_analysis)
    assert consensus_fixture.anomaly_tag in report
    for tag in consensus_fixture.answer_tags:
        assert tag not in report


def test_group_below_minimum_size_holds_nothing_and_reports_the_skip(tmp_path):
    fixture = build_small_consensus_group_fixture(tmp_path, n_packets=3)
    segmented = segment_pdf(fixture.pdf_path)
    analysis = analyze_consensus_anomalies(fixture.pdf_path, segmented)

    assert analysis.holds == {}
    assert len(analysis.skipped_groups) == 1
    skipped = analysis.skipped_groups[0]
    assert skipped.n_items == 3
    assert skipped.min_group_size == 5

    report = format_consensus_report(analysis)
    assert "below the minimum group size" in report
    assert "3 packet(s) available, need >= 5" in report
