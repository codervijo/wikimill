"""URL and domain normalization (prd.md §10).

The governing bias: a false merge is far worse than a missed one, because it
silently attributes one site's liveness to another. So the tests below check
just as hard that we *don't* over-normalize as that we normalize at all.
"""

from __future__ import annotations

import pytest

from wikimill.constants import NORMALIZER_VERSION
from wikimill.normalize import analyse, normalize, url_hash
from wikimill.normalize.url import (
    DropReason,
    normalize_percent_encoding,
    remove_dot_segments,
    strip_tracking,
)


# -- case, ports, fragments -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTP://Example.COM/Path", "http://example.com/Path"),   # path case kept
        ("http://example.com:80/x", "http://example.com/x"),
        ("https://example.com:443/x", "https://example.com/x"),
        ("http://example.com:8080/x", "http://example.com:8080/x"),  # non-default kept
        ("http://example.com/x#frag", "http://example.com/x"),
        ("http://example.com", "http://example.com/"),
        ("http://example.com.", "http://example.com/"),  # trailing dot on host
    ],
)
def test_basic_canonicalization(raw, expected):
    assert normalize(raw).url == expected


def test_path_case_is_preserved():
    """Paths are case-sensitive on most servers; lowercasing them would fetch
    a different resource, or a 404."""
    assert normalize("http://example.com/CaseSensitive").url.endswith("/CaseSensitive")


def test_trailing_slash_is_preserved():
    """`/foo` and `/foo/` are different resources to many servers."""
    assert normalize("http://example.com/foo").url == "http://example.com/foo"
    assert normalize("http://example.com/foo/").url == "http://example.com/foo/"


# -- dot segments + percent encoding ---------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/a/./b/../c", "/a/c"),
        ("/a/b/../../c", "/c"),
        ("/a/b/./", "/a/b/"),
        ("/../a", "/a"),
        ("/a//b", "/a//b"),  # an empty segment is meaningful; do not collapse
    ],
)
def test_remove_dot_segments(path, expected):
    assert remove_dot_segments(path) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("%7Euser", "~user"),      # unreserved: decode
        ("%7euser", "~user"),      # case-insensitive
        ("%2Fslash", "%2Fslash"),  # reserved: keep, uppercase
        ("%2fslash", "%2Fslash"),
        ("%41", "A"),
    ],
)
def test_percent_encoding_normalized(text, expected):
    assert normalize_percent_encoding(text) == expected


def test_encoded_slash_is_not_decoded():
    """Decoding %2F would change the path structure and fetch a different URL."""
    assert "%2F" in normalize("http://example.com/a%2fb").url


# -- query handling ---------------------------------------------------------


def test_tracking_params_stripped():
    result = normalize("http://example.com/a?utm_source=x&id=7&fbclid=y")
    assert "utm_source" not in result.url
    assert "fbclid" not in result.url
    assert "id=7" in result.url


def test_query_order_is_preserved():
    """Sorting is a common 'normalization' that breaks order-sensitive servers
    and buys nothing here."""
    assert normalize("http://example.com/a?z=1&a=2").url.endswith("?z=1&a=2")


def test_untouched_query_is_not_reencoded():
    """Re-encoding a query we did not change risks altering it."""
    raw = "http://example.com/a?b=%20c&d=e+f"
    assert normalize(raw).url.endswith("?b=%20c&d=e+f")


def test_ambiguous_params_are_kept():
    """`ref`, `id`, `source` are load-bearing on real sites — stripping them
    would fetch a different page than the one Wikipedia cited."""
    result = normalize("http://example.com/a?ref=nav&source=rss&id=3")
    for param in ("ref=nav", "source=rss", "id=3"):
        assert param in result.url


def test_strip_tracking_noop_returns_original():
    assert strip_tracking("a=1&b=2") == "a=1&b=2"


# -- archives ---------------------------------------------------------------


def test_wayback_is_unwrapped():
    """Crawling the wrapper would measure the Internet Archive's uptime, not
    the cited domain's."""
    result = normalize(
        "https://web.archive.org/web/20200101000000/https://example.com/page"
    )
    assert result.url == "https://example.com/page"
    assert result.archive_url.startswith("https://web.archive.org/")
    assert result.archive_date == "20200101000000"


def test_wayback_flagged_rendition_is_unwrapped():
    result = normalize(
        "https://web.archive.org/web/20200101000000id_/http://example.com/x"
    )
    assert result.url == "http://example.com/x"


def test_wayback_with_missing_slash_is_repaired():
    """Real wayback URLs contain `https:/example.com`; unrepaired, that yields a
    hostless URL that silently drops out of the queue."""
    result = normalize(
        "https://web.archive.org/web/20200101000000/https:/example.com/x"
    )
    assert result.url == "https://example.com/x"


def test_wayback_preserves_the_origin_query():
    result = normalize(
        "https://web.archive.org/web/20200101/http://example.com/a?b=c"
    )
    assert result.url == "http://example.com/a?b=c"


def test_archive_today_is_unwrapped():
    result = normalize("https://archive.ph/20200101/https://example.com/x")
    assert result.url == "https://example.com/x"


def test_opaque_archive_is_kept_not_guessed():
    """ghostarchive addresses snapshots by opaque ID — there is no origin URL to
    recover, so we keep the wrapper rather than invent one."""
    result = normalize("https://ghostarchive.org/archive/abc123")
    assert result.keep
    assert result.host == "ghostarchive.org"
    assert result.archive_url is None


def test_no_queued_url_is_an_archive_host():
    """Acceptance criterion 8."""
    for raw in (
        "https://web.archive.org/web/20200101000000/https://example.com/a",
        "https://archive.today/20200101/https://example.com/b",
    ):
        assert "archive" not in normalize(raw).host


# -- filtering --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://en.wikipedia.org/wiki/Foo",
        "https://commons.wikimedia.org/wiki/File:X",
        "https://www.wikidata.org/wiki/Q42",
    ],
)
def test_wikimedia_internal_dropped(raw):
    assert normalize(raw).drop_reason is DropReason.WIKIMEDIA_INTERNAL


@pytest.mark.parametrize(
    "raw",
    ["https://doi.org/10.1000/182", "https://hdl.handle.net/123", "http://n2t.net/x"],
)
def test_identifier_resolvers_dropped(raw):
    assert normalize(raw).drop_reason is DropReason.IDENTIFIER_RESOLVER


def test_lookalike_domains_are_not_filtered():
    """Label-boundary matching: `notdoi.org` is not `doi.org`, and
    `wikipedia.org.evil.com` is not Wikimedia."""
    assert normalize("http://notdoi.org/x").keep
    assert normalize("http://wikipedia.org.evil.com/x").keep


@pytest.mark.parametrize("raw", ["mailto:a@b.com", "ftp://example.com/x", "irc://x/y"])
def test_non_crawlable_schemes_dropped(raw):
    assert normalize(raw).drop_reason is DropReason.NOT_CRAWLABLE_SCHEME


def test_hostless_dropped():
    assert normalize("http:///path").drop_reason is DropReason.NO_HOST


def test_empty_dropped():
    assert normalize("").drop_reason is DropReason.MALFORMED


# -- IDN --------------------------------------------------------------------


def test_idn_host_punycoded():
    assert normalize("http://bücher.example/x").host == "xn--bcher-kva.example"


def test_ascii_host_untouched_by_idna():
    assert normalize("http://example.com/x").host == "example.com"


# -- domain analysis --------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "registrable"),
    [
        ("www.housing.berkeley.edu", "berkeley.edu"),
        ("news.bbc.co.uk", "bbc.co.uk"),   # naive last-two-labels gets this wrong
        ("example.com", "example.com"),
        ("a.b.c.example.co.uk", "example.co.uk"),
    ],
)
def test_registrable_domain(host, registrable):
    assert analyse(host).registrable_domain == registrable


@pytest.mark.parametrize("host", ["foo.blogspot.com", "user.github.io"])
def test_private_suffix_flagged(host):
    assert analyse(host).is_private_suffix


@pytest.mark.parametrize("host", ["wbc.poznan.pl", "spb.org.ru", "pdmi.ras.ru"])
def test_private_suffix_does_not_exclude_regional_registries(host):
    """All three appear in real enwiki data and are PSL private-section entries,
    but they are regional/institutional registries — not platforms, and possibly
    registrable. The flag records the fact; it must not silently drop them."""
    info = analyse(host)
    assert info.is_private_suffix
    assert info.acquireable_candidate


def test_ip_host_has_no_registrable_domain():
    info = analyse("66.102.9.104")
    assert info.is_ip
    assert info.registrable_domain == ""
    assert not info.acquireable_candidate


@pytest.mark.parametrize("host", ["doi.org", "en.wikipedia.org"])
def test_only_unambiguous_hosts_are_excluded(host):
    assert not analyse(host).acquireable_candidate


def test_ordinary_domain_is_a_candidate():
    assert analyse("example.com").acquireable_candidate


def test_www_does_not_affect_domain_identity():
    assert (
        analyse("www.example.com").registrable_domain
        == analyse("example.com").registrable_domain
    )


def test_www_is_kept_in_url_identity():
    """It can 404 differently, so the URL keeps it even though the domain
    does not."""
    assert normalize("http://www.example.com/").host == "www.example.com"


# -- identity ---------------------------------------------------------------


def test_hash_is_stable_and_of_the_normalized_form():
    a = normalize("HTTP://Example.com:80/a/./b?utm_source=x#frag")
    b = normalize("http://example.com/a/b")
    assert a.url == b.url
    assert url_hash(a.url) == url_hash(b.url)


def test_normalization_is_deterministic():
    raw = "http://Example.com/%7Euser/../x?utm_medium=y&keep=1"
    assert normalize(raw).url == normalize(raw).url


def test_version_is_stamped():
    assert normalize("http://example.com/").normalizer_version == NORMALIZER_VERSION
