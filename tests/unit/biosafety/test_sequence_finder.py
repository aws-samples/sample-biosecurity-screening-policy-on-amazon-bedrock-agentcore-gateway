# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for research_gateway.biosafety.sequence_finder — the regex-based
amino acid sequence extractor that feeds the biosafety screening pipeline."""

from research_gateway.biosafety.sequence_finder import extract_sequences

VALID_20MER = "ACDEFGHIKLMNPQRSTVWY"  # exactly one of each of the 20 matched letters


def test_extracts_sequence_at_minimum_length():
    assert extract_sequences(VALID_20MER) == [VALID_20MER]


def test_ignores_sequence_below_minimum_length():
    too_short = VALID_20MER[:19]
    assert extract_sequences(too_short) == []


def test_matches_letters_outside_the_20_standard_amino_acids_are_split():
    # B, J, O, U, X, Z are not in the matched character class, so a sequence
    # containing one is split into the surrounding runs instead of matching
    # as one long sequence.
    reversed_20mer = VALID_20MER[::-1]
    sequence = VALID_20MER + "X" + reversed_20mer
    assert extract_sequences(sequence) == [VALID_20MER, reversed_20mer]


def test_case_insensitive_matching():
    assert extract_sequences(VALID_20MER.lower()) == [VALID_20MER]


def test_dedupes_repeated_sequences_preserving_first_seen_order():
    text = f"{VALID_20MER} then later {VALID_20MER} and {VALID_20MER[::-1]}"
    result = extract_sequences(text)
    assert result == [VALID_20MER, VALID_20MER[::-1]]


def test_walks_nested_dict_and_list_structures():
    obj = {
        "a": [VALID_20MER, {"b": VALID_20MER[::-1]}],
        "c": {"d": {"e": [1, 2, VALID_20MER]}},
        "f": None,
        "g": 42,
    }
    result = extract_sequences(obj)
    assert set(result) == {VALID_20MER, VALID_20MER[::-1]}


def test_no_sequences_found_returns_empty_list():
    assert extract_sequences({"query": "SARS-CoV-2 spike protein"}) == []


def test_non_string_non_container_values_are_ignored():
    assert extract_sequences(42) == []
    assert extract_sequences(None) == []
