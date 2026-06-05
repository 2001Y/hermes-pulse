from pathlib import Path

from hermes_pulse.source_registry import load_source_registry


FIXTURE_PATH = Path("fixtures/source_registry/sample_sources.yaml")
LAUNCHER_FIXTURE_PATH = Path("fixtures/source_registry/launcher_sources.yaml")


def test_load_source_registry_from_yaml() -> None:
    entries = load_source_registry(FIXTURE_PATH)

    assert len(entries) == 3

    official = entries[0]
    assert official.authority_tier == "primary"
    assert official.rss_url == "https://example.com/feed.xml"
    assert official.search_hints == ["site:example.com official updates"]
    assert official.requires_primary_confirmation is False

    trusted_secondary = entries[1]
    assert trusted_secondary.authority_tier == "trusted_secondary"
    assert trusted_secondary.rss_url == "https://trusted.example.org/atom.xml"
    assert trusted_secondary.requires_primary_confirmation is True

    discovery_only = entries[2]
    assert discovery_only.authority_tier == "discovery_only"
    assert discovery_only.rss_url is None
    assert discovery_only.search_hints == ["site:discover.example.net rumors"]
    assert discovery_only.requires_primary_confirmation is True


def test_launcher_source_registry_includes_curated_apple_ai_tech_finance_and_ev_sources() -> None:
    entries = load_source_registry(LAUNCHER_FIXTURE_PATH)

    ids = {entry.id for entry in entries}

    assert "apple-newsroom" in ids
    assert "apple-developer-news" in ids
    assert "aapl-ch" in ids
    assert "nine-to-five-mac" in ids
    assert "openai-news" in ids
    assert "anthropic-newsroom" in ids
    assert "xai-news" in ids
    assert "xtech" in ids
    assert "itmedia-news" in ids
    assert "publickey" in ids
    assert "nikkei-markets" in ids
    assert "bloomberg-japan" in ids
    assert "reuters-finance" in ids
    assert "tesla-news" in ids
    assert "electrek" in ids
    assert "insideevs" in ids
    assert "car-watch" in ids
    assert "byd-global" in ids
    assert "hyundai-worldwide" in ids
    assert "kia-global" in ids
    assert "nio-global" in ids
    assert "polestar-news" in ids
    assert "mini-news" in ids
    assert "bmw-group-news" in ids
    assert "motor1" in ids
    assert "carscoops" in ids
    assert "top-gear" in ids
    assert "dpreview" in ids
    assert "petapixel" in ids
    assert "digital-camera-watch" in ids
    assert "canon-rumors" in ids
    assert "nikon-rumors" in ids
    assert "sonyalpha-rumors" in ids
    assert "fuji-rumors" in ids
    assert "leica-rumors" in ids
    assert "photo-rumors" in ids
    assert "mirrorless-rumors" in ids
    assert "43-rumors" in ids
    assert "viltrox-global" in ids
    assert "laowa-global" in ids
    assert "ttartisan-global" in ids
    assert "sevenartisans-global" in ids
    assert "sigma-global" in ids
    assert "tamron-global" in ids
    assert "voigtlander-global" in ids
    assert "zeiss-photography" in ids
    assert "blackmagic-design" in ids
    assert "red-digital-cinema" in ids
    assert "arri-news" in ids
    assert "canon-cinema-eos" in ids
    assert "sony-cine" in ids
    assert "cooke-optics" in ids
    assert "zeiss-cine" in ids
    assert "sigma-cine" in ids
    assert "dzofilm" in ids
    assert "laowa-cine" in ids
    assert "viltrox-cine" in ids
    assert "cined" in ids
    assert "no-film-school" in ids
    assert "newsshooter" in ids
    assert "provideo-coalition" in ids
    assert "frameio-insider" in ids
    assert "studiodaily" in ids
    assert "redshark-news" in ids
    assert "ymcinema-magazine" in ids
    assert "angenieux" in ids
    assert "atlas-lens-co" in ids
    assert "nisi-cine" in ids
    assert "tokina-cinema" in ids
    assert "xeen" in ids
    assert "schneider-kreuznach-cine" in ids
    assert "thypoch" in ids
    assert "meike-cine" in ids
    assert "sirui-cine" in ids
    assert "irix-cine" in ids
    assert "leitz-cine" in ids
    assert "canon-cinema-lens" in ids
    assert "fujinon-cine-broadcast" in ids
    assert "sony-cinema-lens" in ids
    assert "panasonic-leica-lmount-video" in ids
    assert "dulens" in ids
    assert "blazar" in ids
    assert "great-joy" in ids
    assert "arri-rental" not in ids
    assert "toyota-global-newsroom" not in ids
    assert "honda-global-newsroom" not in ids
    assert "nissan-newsroom" not in ids
    assert len(entries) >= 82


def test_launcher_source_registry_reuses_default_registry_entries_via_include() -> None:
    payload = LAUNCHER_FIXTURE_PATH.read_text()

    assert "include:" in payload
    assert "default_sources.yaml" in payload

    entries = load_source_registry(LAUNCHER_FIXTURE_PATH)
    default_entries = {entry.id: entry for entry in load_source_registry(Path("fixtures/source_registry/default_sources.yaml"))}

    launcher_entry = next(entry for entry in entries if entry.id == "aapl-ch")
    default_entry = default_entries["aapl-ch"]

    assert launcher_entry == default_entry


def test_launcher_source_registry_prefers_direct_feeds_or_sitemaps_for_sources_that_triggered_search_errors() -> None:
    entries = {entry.id: entry for entry in load_source_registry(LAUNCHER_FIXTURE_PATH)}
    direct_sources = {
        "digital-camera-watch": "https://dc.watch.impress.co.jp/data/rss/1.0/dcw/feed.rdf",
        "canon-rumors": "https://www.canonrumors.com/feed/",
        "nikon-rumors": "https://nikonrumors.com/feed/",
        "sonyalpha-rumors": "https://www.sonyalpharumors.com/feed/",
        "fuji-rumors": "https://www.fujirumors.com/feed/",
        "leica-rumors": "https://leicarumors.com/feed/",
        "photo-rumors": "https://photorumors.com/feed/",
        "mirrorless-rumors": "https://www.mirrorlessrumors.com/feed/",
        "43-rumors": "https://www.43rumors.com/feed/",
        "viltrox-global": "https://viltrox.com/sitemap.xml",
        "ttartisan-global": "https://www.ttartisan.com/sitemap.xml",
        "sevenartisans-global": "https://7artisans.com/sitemap.xml",
        "tamron-global": "https://www.tamron.com/sitemap.xml",
        "voigtlander-global": "https://www.voigtlaender.de/feed",
        "zeiss-photography": "https://www.zeiss.com/sitemap.xml",
        "blackmagic-design": "https://www.blackmagicdesign.com/rss",
        "red-digital-cinema": "https://www.red.com/sitemap.xml",
        "arri-news": "https://www.arri.com/service-sitemap-9ab01b4faec684298d4d23a8493d135a-sitemap_index.xml",
        "cooke-optics": "https://www.cookeoptics.com/feed",
        "zeiss-cine": "https://www.zeiss.com/sitemap.xml",
        "viltrox-cine": "https://viltrox.com/sitemap.xml",
        "cined": "https://www.cined.com/feed/",
        "newsshooter": "https://www.newsshooter.com/feed/",
        "provideo-coalition": "https://www.provideocoalition.com/feed/",
        "studiodaily": "https://www.studiodaily.com/feed/",
        "nisi-cine": "https://nisiopticsusa.com/feed/",
        "meike-cine": "https://meikeglobal.com/sitemap.xml",
        "sirui-cine": "https://store.sirui.com/sitemap.xml",
    }
    google_news_fallback_sources = {
        "laowa-global",
        "canon-cinema-eos",
        "sony-cine",
        "laowa-cine",
        "angenieux",
        "tokina-cinema",
        "xeen",
        "canon-cinema-lens",
        "sony-cinema-lens",
    }

    for source_id, rss_url in direct_sources.items():
        assert entries[source_id].acquisition_mode == "rss_poll", source_id
        assert entries[source_id].rss_url == rss_url, source_id

    for source_id in google_news_fallback_sources:
        entry = entries[source_id]
        assert entry.domain == "news.google.com", source_id
        assert entry.acquisition_mode == "rss_poll", source_id
        assert entry.authority_tier == "discovery_only", source_id
        assert entry.category_hint == "camera", source_id
        assert entry.rss_url is not None and entry.rss_url.startswith("https://news.google.com/rss/search?q="), source_id
        assert "google-news" in entry.topical_scopes, source_id
        assert entry.requires_primary_confirmation is True, source_id


def test_launcher_source_registry_includes_google_news_feeds_for_digest_categories() -> None:
    entries = {entry.id: entry for entry in load_source_registry(LAUNCHER_FIXTURE_PATH)}
    expected_categories = {
        "google-news-ai": "ai",
        "google-news-it": "it",
        "google-news-finance": "finance",
        "google-news-camera": "camera",
        "google-news-car": "car",
    }

    for source_id, category in expected_categories.items():
        entry = entries[source_id]
        assert entry.domain == "news.google.com"
        assert entry.acquisition_mode == "rss_poll"
        assert entry.authority_tier == "discovery_only"
        assert entry.category_hint == category
        assert entry.rss_url is not None
        assert entry.rss_url.startswith("https://news.google.com/rss/search?q=")
        assert "OR" in entry.rss_url
        assert {"google-news", category}.issubset(set(entry.topical_scopes))
