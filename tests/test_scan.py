"""Tests for scan.py — pair generation, JSON extraction, filter mapping."""

from unittest.mock import MagicMock, patch

from scan import (
    ENTITY_BLOCKLIST,
    _call_llm,
    _call_openai_compat,
    _extract_json,
    _FILTER_TO_API_TAG,
    filter_groups_by_sport,
    format_pair_for_llm,
    generate_candidate_pairs,
    rule_screen_pair,
    rule_screen_pairs,
)


def _market(ticker, series, event="E1", entity="Player A", sport="Tennis", sub_sport="Tennis"):
    return {
        "ticker": ticker,
        "series_ticker": series,
        "event_ticker": event,
        "title": f"Title {ticker}",
        "rules_primary": "Rules",
        "yes_sub_title": entity,
        "sport_tag": sport,
        "sub_sport": sub_sport,
        "volume": 500,
    }


# ── generate_candidate_pairs ─────────────────────────────────────────────────


def test_cross_series_pairing():
    groups = {
        "Alcaraz": [
            _market("T1", "FO", event="E1"),
            _market("T2", "GS", event="E2"),
        ]
    }
    pairs = generate_candidate_pairs(groups)
    assert len(pairs) == 1
    tickers = {pairs[0][0]["ticker"], pairs[0][1]["ticker"]}
    assert tickers == {"T1", "T2"}


def test_same_series_paired():
    """generate_candidate_pairs does not filter by series; that's handled upstream."""
    groups = {
        "Alcaraz": [
            _market("T1", "FO", event="E1"),
            _market("T2", "FO", event="E2"),
        ]
    }
    pairs = generate_candidate_pairs(groups)
    assert len(pairs) == 1


def test_same_event_paired():
    """generate_candidate_pairs does not filter by event; that's handled upstream."""
    groups = {
        "Alcaraz": [
            _market("T1", "S1", event="E1"),
            _market("T2", "S2", event="E1"),
        ]
    }
    pairs = generate_candidate_pairs(groups)
    assert len(pairs) == 1


def test_cross_sport_rejected():
    groups = {
        "Player": [
            _market("T1", "S1", event="E1", sub_sport="Tennis"),
            _market("T2", "S2", event="E2", sub_sport="Golf"),
        ]
    }
    pairs = generate_candidate_pairs(groups)
    assert len(pairs) == 0


def test_blocklisted_entity():
    for entity in ("Tie", "Yes"):
        assert entity in ENTITY_BLOCKLIST
    groups = {
        "Tie": [
            _market("T1", "S1", event="E1", entity="Tie"),
            _market("T2", "S2", event="E2", entity="Tie"),
        ]
    }
    pairs = generate_candidate_pairs(groups)
    assert len(pairs) == 0


def test_empty_groups():
    assert generate_candidate_pairs({}) == []


def test_multiple_entities():
    groups = {
        "Alcaraz": [
            _market("T1", "S1", event="E1"),
            _market("T2", "S2", event="E2"),
        ],
        "Sinner": [
            _market("T3", "S1", event="E3", entity="Sinner"),
            _market("T4", "S3", event="E4", entity="Sinner"),
        ],
    }
    pairs = generate_candidate_pairs(groups)
    assert len(pairs) == 2


# ── format_pair_for_llm ──────────────────────────────────────────────────────


def test_format_pair_for_llm():
    a = _market("TICK-A", "S1")
    b = _market("TICK-B", "S2")
    text = format_pair_for_llm(1, a, b)
    assert "Pair 1" in text
    assert "TICK-A" in text
    assert "TICK-B" in text
    assert "Event A:" in text
    assert "Event B:" in text


def test_format_pair_truncates_rules():
    a = _market("T1", "S1")
    a["rules_primary"] = "x" * 1000
    b = _market("T2", "S2")
    text = format_pair_for_llm(1, a, b)
    # rules truncated to 500 chars
    assert "x" * 501 not in text


# ── _extract_json ────────────────────────────────────────────────────────────


def test_extract_json_plain():
    text = '{"results": [{"ticker_a": "A", "confidence": "high"}]}'
    result = _extract_json(text)
    assert len(result) == 1
    assert result[0]["ticker_a"] == "A"


def test_extract_json_markdown_fenced():
    text = '```json\n{"results": [{"ticker_a": "A"}]}\n```'
    result = _extract_json(text)
    assert len(result) == 1


def test_extract_json_pairs_key():
    text = '{"pairs": [{"ticker_a": "X"}]}'
    result = _extract_json(text)
    assert result[0]["ticker_a"] == "X"


def test_extract_json_data_key():
    text = '{"data": [{"ticker_a": "X"}]}'
    result = _extract_json(text)
    assert result[0]["ticker_a"] == "X"


def test_extract_json_single_object():
    text = '{"antecedent_ticker": "A", "consequent_ticker": "B"}'
    result = _extract_json(text)
    assert len(result) == 1
    assert result[0]["antecedent_ticker"] == "A"


def test_extract_json_bare_array():
    text = '[{"ticker_a": "A"}, {"ticker_a": "B"}]'
    result = _extract_json(text)
    assert len(result) == 2


def test_extract_json_malformed():
    import pytest
    import json
    with pytest.raises(json.JSONDecodeError):
        _extract_json("not json at all")


def test_extract_json_unrecognized_dict():
    """Valid JSON but not a recognizable results shape must raise, not
    leak a dict to the caller (which would crash the batch loop)."""
    import pytest
    with pytest.raises(ValueError):
        _extract_json('{"error": "something went wrong"}')


def test_extract_json_list_of_non_dicts():
    import pytest
    with pytest.raises(ValueError):
        _extract_json('["a", "b"]')


def test_extract_json_prose_wrapped():
    """Open-weight models sometimes wrap the JSON in prose; the outermost
    {...} span should still parse."""
    text = 'Here are the results:\n{"results": [{"ticker_a": "A"}]}\nHope that helps!'
    result = _extract_json(text)
    assert result[0]["ticker_a"] == "A"


# ── rule_screen_pair ─────────────────────────────────────────────────────────


def _golf(series, tourn, player="SCHE"):
    return {"ticker": f"{series}-{tourn}-{player}", "series_ticker": series,
            "event_ticker": f"{series}-{tourn}"}


def test_rule_lattice_same_tournament_high():
    r = rule_screen_pair(_golf("KXPGATOP10", "MAST26"), _golf("KXPGATOUR", "MAST26"))
    assert r["confidence"] == "high"
    # Narrower market (winner) is the antecedent regardless of input order
    assert r["antecedent_ticker"] == "KXPGATOUR-MAST26-SCHE"
    assert r["consequent_ticker"] == "KXPGATOP10-MAST26-SCHE"


def test_rule_lattice_full_chain():
    r = rule_screen_pair(_golf("KXPGATOP20", "MAST26"), _golf("KXPGAMAKECUT", "MAST26"))
    assert r["confidence"] == "high"
    assert r["antecedent_ticker"].startswith("KXPGATOP20")


def test_rule_cross_tournament_none():
    r = rule_screen_pair(_golf("KXPGATOP5", "MAST26"), _golf("KXPGATOP10", "VAC26"))
    assert r["confidence"] == "none"
    assert r["antecedent_ticker"] is None


def test_rule_cross_lattice_none():
    # Round-1 position vs final result: no implication either way
    r = rule_screen_pair(_golf("KXPGAR1LEAD", "MAST26"), _golf("KXPGATOUR", "MAST26"))
    assert r["confidence"] == "none"


def test_rule_defers_non_lattice_series():
    # Judgment families (season aggregates, R2 leader) go to the LLM
    assert rule_screen_pair(_golf("KXPGATOUR", "MAST26"), _golf("KXPGAMAJORWIN", "26")) is None
    assert rule_screen_pair(_golf("KXPGAR2LEAD", "MAST26"), _golf("KXPGAMAKECUT", "MAST26")) is None


def test_rule_liv_lattice():
    r = rule_screen_pair(_golf("KXLIVTOUR", "LIGSA26"), _golf("KXLIVTOP10", "LIGSA26"))
    assert r["confidence"] == "high"
    assert r["antecedent_ticker"].startswith("KXLIVTOUR")


def test_rule_screen_pairs_split():
    pairs = [
        (_golf("KXPGATOP5", "MAST26"), _golf("KXPGATOP10", "MAST26")),
        (_market("T1", "S1"), _market("T2", "S2")),
    ]
    results, remaining = rule_screen_pairs(pairs)
    assert len(results) == 1 and results[0]["confidence"] == "high"
    assert len(remaining) == 1 and remaining[0][0]["ticker"] == "T1"


def test_numeric_entity_blocked():
    groups = {"6": [
        _market("TOT1", "KXNHLTOTAL", event="E1", entity="6"),
        _market("TOT2", "KXNHLTOTAL", event="E2", entity="6"),
    ]}
    assert generate_candidate_pairs(groups) == []


# ── LLM backend routing ──────────────────────────────────────────────────────


@patch("scan._call_openai_compat", return_value="oai")
@patch("scan._call_anthropic", return_value="ant")
def test_call_llm_routes_to_anthropic_by_default(mock_ant, mock_oai, monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert _call_llm("prompt", "claude-sonnet-4-6") == "ant"
    mock_oai.assert_not_called()


@patch("scan._call_openai_compat", return_value="oai")
@patch("scan._call_anthropic", return_value="ant")
def test_call_llm_routes_to_openai_compat_when_base_url_set(mock_ant, mock_oai, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://example-inference.do-infra.ai")
    assert _call_llm("prompt", "openai/gpt-oss-120b") == "oai"
    mock_ant.assert_not_called()


@patch("scan.requests.post")
def test_call_openai_compat_parses_chat_completion(mock_post, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "di_test")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": '{"results": []}'}}]
    }
    mock_post.return_value = mock_resp
    out = _call_openai_compat("prompt", "openai/gpt-oss-120b", "https://ep.example/")
    assert out == '{"results": []}'
    url = mock_post.call_args[0][0]
    assert url == "https://ep.example/v1/chat/completions"
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer di_test"


@patch("scan.requests.post")
def test_call_openai_compat_none_content_returns_empty(mock_post, monkeypatch):
    # Reasoning models can exhaust max_tokens mid-thought, leaving content null.
    monkeypatch.setenv("LLM_API_KEY", "di_test")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"choices": [{"message": {"content": None}}]}
    mock_post.return_value = mock_resp
    assert _call_openai_compat("prompt", "m", "https://ep.example") == ""


# ── _FILTER_TO_API_TAG ───────────────────────────────────────────────────────


def test_filter_tag_pro_football():
    assert _FILTER_TO_API_TAG["pro football"] == "Football"


def test_filter_tag_college_football():
    assert _FILTER_TO_API_TAG["college football"] == "Football"


# ── filter_groups_by_sport ───────────────────────────────────────────────────


def test_filter_drops_non_matching_markets():
    """Filtering 'hockey' on a mixed entity keeps only hockey markets."""
    groups = {
        "Denver": [
            _market("NHL-DEN", "KNHL", event="E1", entity="Denver", sport="Hockey", sub_sport="NHL"),
            _market("NFL-DEN", "KNFL", event="E2", entity="Denver", sport="Football", sub_sport="Pro Football"),
            _market("NFL2-DEN", "KNFLPLAY", event="E3", entity="Denver", sport="Football", sub_sport="Pro Football"),
        ]
    }
    filtered = filter_groups_by_sport(groups, ["hockey"])
    assert "Denver" in filtered
    assert len(filtered["Denver"]) == 1
    assert filtered["Denver"][0]["ticker"] == "NHL-DEN"


def test_filter_drops_entity_with_no_matches():
    groups = {
        "Denver": [
            _market("NFL-DEN", "KNFL", event="E1", entity="Denver", sport="Football", sub_sport="Pro Football"),
        ]
    }
    filtered = filter_groups_by_sport(groups, ["hockey"])
    assert "Denver" not in filtered


def test_filter_keeps_tagless_markets():
    groups = {
        "Denver": [
            _market("NHL-DEN", "KNHL", event="E1", entity="Denver", sport="Hockey", sub_sport="NHL"),
            _market("REC-DEN", "KXRECORD", event="E2", entity="Denver", sport="", sub_sport=""),
        ]
    }
    filtered = filter_groups_by_sport(groups, ["hockey"])
    assert len(filtered["Denver"]) == 2


def test_filter_mixed_entity_no_cross_sport_pairs():
    """End-to-end: filtering + pair generation produces no cross-sport pairs."""
    groups = {
        "Denver": [
            _market("NHL1-DEN", "KNHL", event="E1", entity="Denver", sport="Hockey", sub_sport="NHL"),
            _market("NHL2-DEN", "KNHLPLAY", event="E2", entity="Denver", sport="Hockey", sub_sport="NHL"),
            _market("NFL1-DEN", "KNFL", event="E3", entity="Denver", sport="Football", sub_sport="Pro Football"),
            _market("NFL2-DEN", "KNFLPLAY", event="E4", entity="Denver", sport="Football", sub_sport="Pro Football"),
        ]
    }
    filtered = filter_groups_by_sport(groups, ["hockey"])
    pairs = generate_candidate_pairs(filtered)
    assert len(pairs) == 1
    tickers = {pairs[0][0]["ticker"], pairs[0][1]["ticker"]}
    assert tickers == {"NHL1-DEN", "NHL2-DEN"}
