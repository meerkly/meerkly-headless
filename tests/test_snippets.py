from meerkly_headless import snippets


def test_settle_observer_excludes_attributes():
    """The single most load-bearing detail: attribute churn (CSS animations,
    class toggles) must not keep resetting the quiet timer forever."""
    assert "childList: true" in snippets.SETTLE_STABLE
    assert "characterData: true" in snippets.SETTLE_STABLE
    assert "attributes" not in snippets.SETTLE_STABLE


def test_settle_quiet_window_is_500ms():
    assert "const QUIET = 500;" in snippets.SETTLE_STABLE


def test_selector_snippets_do_observe_attributes():
    """Visibility usually changes via an attribute, so these must watch them."""
    assert "attributes: true" in snippets.PROBE_RULES
    assert "attributes: true" in snippets.WAIT_SELECTOR_VISIBLE


def test_visibility_predicate_is_shared():
    marker = "getComputedStyle(el).visibility !== 'hidden'"
    assert marker in snippets.PROBE_RULES
    assert marker in snippets.WAIT_SELECTOR_VISIBLE


def test_throwing_selector_asymmetry():
    # A bad guard should simply never match, so probing continues.
    assert "catch (e) { el = null; }" in snippets.PROBE_RULES
    # A bad target selector can never become visible, so give up at once.
    assert "catch (e) { finish(true); return; }" in snippets.WAIT_SELECTOR_VISIBLE


def test_all_snippets_are_arrow_functions():
    for snippet in (
        snippets.PROBE_RULES,
        snippets.WAIT_SELECTOR_VISIBLE,
        snippets.SETTLE_STABLE,
        snippets.EXTRACT_HTML,
    ):
        assert "=>" in snippet
        assert snippet.strip().startswith("(")


def test_extract_html_caps_output():
    assert "documentElement.outerHTML" in snippets.EXTRACT_HTML
    assert "slice(0, cap)" in snippets.EXTRACT_HTML
