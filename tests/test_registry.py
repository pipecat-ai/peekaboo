import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from macos.registry import (  # noqa: E402
    EventKind,
    Window,
    content_windows,
    diff,
    find_app,
    find_window,
    with_title,
)
from macos.registry import App, collapse_tabs  # noqa: E402


def window(id, title, app="Ghostty", bundle="com.mitchellh.ghostty", pid=100, size=(800, 600), on_screen=True, layer=0):
    return Window(
        id=id,
        title=title,
        app=app,
        bundle_id=bundle,
        pid=pid,
        frame=(0.0, 0.0, float(size[0]), float(size[1])),
        on_screen=on_screen,
        layer=layer,
    )


def test_content_windows_drops_helpers_ours_and_denied():
    ours = window(1, "peekaboo", pid=42)
    tiny = window(2, "", size=(20, 20))
    menu = window(3, "", layer=25)
    vault = window(4, "1Password", app="1Password", bundle="com.1password.1password")
    real = window(5, "tmux")
    kept = content_windows([ours, tiny, menu, vault, real], own_pid=42)
    assert kept == [ours, real]  # our own memories window is a regular window
    # With the regular-app set given, an agent's window (pid 7) is out too.
    agent = window(6, "Creative Cloud Desktop", app="Creative Cloud", bundle="com.adobe.acc", pid=7)
    assert content_windows([real, agent], own_pid=42, regular_pids={100}) == [real]


def test_diff_reports_open_close_retitle_and_visibility():
    a = window(1, "~")
    b = window(2, "build", on_screen=False)
    old = {1: a, 2: b}
    new = {1: with_title(a, "make test"), 3: window(3, "Discord")}
    events = diff(old, new)
    kinds = {(e.kind, e.window.id) for e in events}
    assert kinds == {(EventKind.RETITLED, 1), (EventKind.OPENED, 3), (EventKind.CLOSED, 2)}
    retitle = next(e for e in events if e.kind == EventKind.RETITLED)
    assert retitle.previous.title == "~" and retitle.window.title == "make test"

    shown = diff({2: b}, {2: Window(**{**b.__dict__, "on_screen": True})})
    assert [e.kind for e in shown] == [EventKind.SHOWN]


def test_find_window_prefers_exact_app_then_title_then_biggest_on_screen():
    chrome_small = window(1, "Terminal docs", app="Google Chrome", bundle="com.google.Chrome", size=(300, 300))
    terminal = window(2, "~", app="Terminal", bundle="com.apple.Terminal")
    ghostty_off = window(3, "tmux", size=(1700, 1000), on_screen=False)
    ghostty_on = window(4, "tmux", size=(1200, 800))
    windows = [chrome_small, terminal, ghostty_off, ghostty_on]

    # Exact app name beats a title that merely contains the word.
    assert find_window(windows, "terminal") is terminal
    # A title substring; on screen beats bigger but off screen.
    assert find_window(windows, "tmux") is ghostty_on
    # App substring as a last resort.
    assert find_window(windows, "chrome") is chrome_small
    assert find_window(windows, "nothing here") is None
    assert find_window(windows, "  ") is None


def test_aliases_and_filler_words():
    ghostty = window(1, "tmux", app="Ghostty", bundle="com.mitchellh.ghostty")
    chrome = window(2, "Docs", app="Google Chrome", bundle="com.google.Chrome")
    windows = [ghostty, chrome]

    assert find_window(windows, "the terminal") is ghostty
    assert find_window(windows, "my terminal window") is ghostty
    assert find_window(windows, "the browser") is chrome
    assert find_window(windows, "the Chrome window") is chrome
    # A real name still beats an alias.
    terminal = window(3, "~", app="Terminal", bundle="com.apple.Terminal")
    assert find_window(windows + [terminal], "terminal") is terminal
    assert find_app([App("Ghostty", "com.mitchellh.ghostty", 1)], "the terminal app").name == "Ghostty"


def test_find_app_exact_before_substring():
    apps = [App("Google Chrome", "com.google.Chrome", 1), App("Chrome Helper", "com.google.helper", 2)]
    assert find_app(apps, "chrome").name == "Google Chrome"
    assert find_app(apps, "helper").name == "Chrome Helper"
    assert find_app(apps, "zoom") is None


def test_watchlist_for_binds_items_to_targets():
    from processors.vision import WatchItem, watchlist_for

    everywhere = WatchItem(0, "a meeting banner")
    terminal = WatchItem(1, "the build finishes", target="window:5")
    chrome = WatchItem(2, "a new message", target="window:9")

    assert watchlist_for([chrome, terminal, everywhere], "window:5") == [everywhere, terminal]
    assert watchlist_for([chrome, terminal, everywhere], "screen") == [everywhere]


def test_collapse_tabs_folds_same_frame_siblings_into_the_visible_one():
    tab1 = window(1, "~", on_screen=False, size=(1728, 1084))
    front = window(2, "tmux", on_screen=True, size=(1728, 1084))
    tab2 = window(3, "aleix@mac:~", on_screen=False, size=(1728, 1084))
    other = window(4, "notes", size=(800, 600))
    chrome = window(5, "Docs", app="Google Chrome", bundle="com.google.Chrome", pid=200, on_screen=False, size=(1728, 1084))

    folded = collapse_tabs([tab1, front, tab2, other, chrome])
    assert [w.id for w in folded] == [2, 4, 5]
    assert folded[0].tabs == ("~", "aleix@mac:~")
    assert folded[2].tabs == ()  # a different app with the same frame is not a tab


def test_find_window_by_id_is_exact():
    from macos.registry import Window, find_window

    windows = [
        Window(id=10, title="Threads - Daily - 3 new items - Slack", app="Slack", bundle_id="com.tinyspeck.slackmacgap", pid=1, frame=(0, 0, 800, 600), on_screen=True),
        Window(id=11, title="tmux", app="Ghostty", bundle_id="com.mitchellh.ghostty", pid=2, frame=(0, 0, 800, 600), on_screen=True),
    ]
    assert find_window(windows, "window:10").id == 10
    assert find_window(windows, "window:99") is None
    assert find_window(windows, "slack").id == 10


def test_split_wanted_reads_the_pickers_app_and_title():
    from workers.screen import _split_wanted

    assert _split_wanted("Slack: Threads - Daily") == ("Slack", "Threads - Daily")
    assert _split_wanted("Slack: ! ext-daily: Channel") == ("Slack", "! ext-daily: Channel")
    assert _split_wanted("the terminal") == ("", "")


def test_exclusions_setting_is_normalized():
    from exclusions import DEFAULT_EXCLUDED_APPS, bundle_ids, normalize

    assert "com.1password.1password" in bundle_ids(DEFAULT_EXCLUDED_APPS)
    cleaned = normalize([{"bundle_id": "com.a", "name": "A"}, "com.b", {"bundle_id": "com.a", "name": "again"}, {"name": "no id"}, 7])
    assert cleaned == [{"bundle_id": "com.a", "name": "A"}, {"bundle_id": "com.b", "name": "com.b"}]
    assert normalize("garbage") == []


def test_api_errors_are_explained_in_one_sentence():
    from models import ModelChoice, explain_error

    anthropic = ModelChoice("anthropic", "claude-haiku-4-5")
    assert explain_error("Error code: 401 - {'type': 'authentication_error', 'message': 'invalid x-api-key'}", anthropic) == "The Anthropic API key isn't valid. Check it in Settings."
    assert "rate-limiting" in explain_error("Error code: 429 - rate_limit_error", anthropic)
    assert "claude-haiku-4-5" in explain_error("Error code: 404 - model not_found", anthropic)
    assert "reach Anthropic" in explain_error("Connection error: timed out", anthropic)
    assert explain_error("something odd", ModelChoice("openai", "gpt-4.1")) == "Something went wrong with OpenAI."
