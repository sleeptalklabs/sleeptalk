from sleeptalk.voice.fillers import FillerCatalog, FirstTokenFiller, SpeculationStats


def test_filler_catalog_selects_by_category_and_uses_configured_lists():
    catalog = FillerCatalog.from_strings(
        general="General one.|General two.",
        web_search="Search one.|Search two.",
    )

    assert catalog.select("general", 1) == "General two."
    assert catalog.select("web_search", 0) == "Search one."
    assert catalog.select("unknown", 2) == "General one."


def test_first_token_filler_emits_once_after_wall_clock_budget():
    filler = FirstTokenFiller(
        budget_ms=800,
        category="web_search",
        catalog=FillerCatalog.from_strings(web_search="Checking."),
        started_at=10.0,
    )

    assert filler.due(10.799) is False
    assert filler.due(10.8) is True
    assert filler.emit() == "Checking."
    assert filler.due(11.0) is False


def test_speculation_stats_reports_discard_rate_and_token_overhead():
    stats = SpeculationStats()

    stats.record_attempt(estimated_tokens=12)
    stats.record_discard()
    stats.record_attempt(estimated_tokens=3)
    stats.record_accept()

    assert stats.discard_rate == 0.5
    assert stats.token_overhead == 15
