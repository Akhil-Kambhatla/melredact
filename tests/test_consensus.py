import numpy as np
import pytest

from melredact.config import (
    CONSENSUS_BLOCK_PX,
    CONSENSUS_REGISTRATION_TOLERANCE_BLOCKS,
    CONSENSUS_WRITING_ZONE_DILATION_PT,
    CONSENSUS_WRITING_ZONE_MIN_SHARE,
)
from melredact.consensus import _zone_params, analyze_consensus_anomalies, flagged_regions, format_consensus_report
from melredact.pipeline import packet_tag
from melredact.segment import segment_pdf
from tests.make_fixture import (
    build_consensus_fixture,
    build_consensus_writing_zone_fixture,
    build_small_consensus_group_fixture,
)


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


@pytest.fixture(scope="module")
def zone_fixture(tmp_path_factory):
    return build_consensus_writing_zone_fixture(tmp_path_factory.mktemp("consensus_zone_fixture"))


@pytest.fixture(scope="module")
def zone_segmented(zone_fixture):
    return segment_pdf(zone_fixture.pdf_path)


@pytest.fixture(scope="module")
def zone_analysis(zone_fixture, zone_segmented):
    return analyze_consensus_anomalies(zone_fixture.pdf_path, zone_segmented)


def test_ragged_answer_positions_inside_the_shared_zone_are_never_held(zone_fixture, zone_analysis):
    for tag in zone_fixture.zone_tags:
        assert zone_analysis.holds_for(tag) == [], f"{tag} should not be held -- inside the shared writing zone"


def test_mark_at_the_ragged_edge_of_a_shared_zone_is_not_held(zone_fixture, zone_analysis):
    assert zone_analysis.holds_for(zone_fixture.ragged_edge_tag) == []


def test_isolated_margin_mark_far_outside_the_zone_is_always_held(zone_fixture, zone_analysis):
    holds = zone_analysis.holds_for(zone_fixture.margin_leak_tag)
    assert len(holds) == 1
    assert "page 2" in holds[0].reason


def test_registration_jitter_on_printed_text_does_not_flag_with_tolerance(zone_fixture):
    """Direct pass-one unit test (not the fixture's rendered rasters): a
    printed line's blocks shifted by one row, with a modest per-copy
    density difference on top (0.55 vs the group's 0.5 -- the "per-copy
    density variation" from the task), must not flag once the registration
    tolerance is applied, but must flag at tolerance=0 -- proving the
    tolerance is what's doing the work, not the density gap alone."""
    median = np.zeros((10, 10), dtype=np.float64)
    median[5, 2:8] = 0.5
    dens_i = np.zeros((10, 10), dtype=np.float64)
    dens_i[6, 2:8] = 0.55

    assert flagged_regions(dens_i, median, CONSENSUS_BLOCK_PX, tolerance_blocks=0) != []
    assert flagged_regions(dens_i, median, CONSENSUS_BLOCK_PX, tolerance_blocks=CONSENSUS_REGISTRATION_TOLERANCE_BLOCKS) == []


def test_zone_params_are_read_from_config_not_hardcoded():
    default_dilation, default_share = _zone_params("SOME_UNCALIBRATED_TYPE")
    assert (default_dilation, default_share) == _zone_params("ANOTHER_UNCALIBRATED_TYPE")

    CONSENSUS_WRITING_ZONE_DILATION_PT["__TEST_TYPE__"] = default_dilation + 1000.0
    CONSENSUS_WRITING_ZONE_MIN_SHARE["__TEST_TYPE__"] = default_share + 7
    try:
        dilation, share = _zone_params("__TEST_TYPE__")
        assert dilation == default_dilation + 1000.0
        assert share == default_share + 7
    finally:
        del CONSENSUS_WRITING_ZONE_DILATION_PT["__TEST_TYPE__"]
        del CONSENSUS_WRITING_ZONE_MIN_SHARE["__TEST_TYPE__"]


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
