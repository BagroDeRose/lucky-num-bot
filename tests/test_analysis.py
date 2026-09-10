from __future__ import annotations

from app.analysis.engine import analyze, reduce_to_single_digit


def test_known_number_2200373() -> None:
    result = analyze("2200373")
    assert result.digit_sum == 17
    assert result.reduced_number == 8
    assert result.normalized_number == "2200373"


def test_known_number_repeated_pairs() -> None:
    result = analyze("2200373")
    assert "22" in result.repeated_pairs
    assert "00" in result.repeated_pairs


def test_repeated_digit_detection() -> None:
    result = analyze("7717711")
    assert 7 in result.repeated_digits


def test_leading_zeroes_preserved_in_digits() -> None:
    result = analyze("0012345")
    assert result.normalized_number == "0012345"
    assert result.digits[0] == 0
    assert result.digits[1] == 0


def test_determinism_same_input_same_output() -> None:
    a = analyze("2200373")
    b = analyze("2200373")
    assert a.model_dump() == b.model_dump()


def test_algorithm_version_present() -> None:
    result = analyze("2200373")
    assert result.algorithm_version == "1.0"


def test_reduce_to_single_digit() -> None:
    assert reduce_to_single_digit(17) == 8
    assert reduce_to_single_digit(9) == 9
    assert reduce_to_single_digit(0) == 0
    assert reduce_to_single_digit(19) == 1


def test_palindrome_pattern_detected() -> None:
    result = analyze("123454321")
    names = {p.name for p in result.detected_patterns}
    assert "palindrome" in names


def test_ascending_sequence_pattern_detected() -> None:
    result = analyze("1234999")
    names = {p.name for p in result.detected_patterns}
    assert "ascending_sequence" in names


def test_analysis_payload_round_trip() -> None:
    from app.analysis.models import AnalysisResult

    result = analyze("2200373")
    payload = result.model_dump_public()
    restored = AnalysisResult.model_validate(payload)
    assert restored.overall_score == result.overall_score
    assert restored.normalized_number == result.normalized_number
