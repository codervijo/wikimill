"""URL reconstruction from el_to_domain_index + el_to_path.

Every case marked "real" is copied verbatim from enwiki-20260701-externallinks
(sampled 2026-07-25). This is the highest-risk parsing in the project: getting it
subtly wrong corrupts every domain in the database, silently.
"""

from __future__ import annotations

import pytest

from wikimill.wiki.eldomain import (
    ReconstructionError,
    reconstruct,
    unreverse_host,
)


@pytest.mark.parametrize(
    ("domain_index", "path", "expected"),
    [
        # -- real rows -----------------------------------------------------
        (
            "http://edu.berkeley.housing.www.",
            "/housing/",
            "http://www.housing.berkeley.edu/housing/",
        ),
        ("http://uk.co.bbc.news.", "/sport1/hi/football/fa_cup/results/default.stm",
         "http://news.bbc.co.uk/sport1/hi/football/fa_cup/results/default.stm"),
        (
            "http://com.rootsweb.genealogy.freepages.",
            "/~vanrcwisner/hilllima.html",
            "http://freepages.genealogy.rootsweb.com/~vanrcwisner/hilllima.html",
        ),
        ("http://com.imdb.www.", "/name/nm0004517/bio",
         "http://www.imdb.com/name/nm0004517/bio"),
        ("http://ch.kloster-einsiedeln.www.", "/", "http://www.kloster-einsiedeln.ch/"),
        ("http://org.combinatorics.www.", "/", "http://www.combinatorics.org/"),
        # -- real: multi-level ccTLD ---------------------------------------
        ("https://uk.ac.ox.www.", "/x", "https://www.ox.ac.uk/x"),
        # -- real: no path (blob is NULL) ----------------------------------
        ("http://com.example.www.", None, "http://www.example.com"),
    ],
)
def test_reconstruct_real_shapes(domain_index, path, expected):
    assert reconstruct(domain_index, path).url == expected


# -- ports: the trap is that they follow the trailing dot -------------------


@pytest.mark.parametrize(
    ("domain_index", "expected_host", "expected_port"),
    [
        ("http://uk.co.linearb.:8080", "linearb.co.uk", 8080),          # real
        ("http://org.homelinux.jadesukka.:8180", "jadesukka.homelinux.org", 8180),
        ("http://fr.ens.eleves.www.:8080", "www.eleves.ens.fr", 8080),  # real
        ("http://edu.wustl.econwpa.:8089", "econwpa.wustl.edu", 8089),  # real
    ],
)
def test_port_is_split_before_labels_are_reversed(
    domain_index, expected_host, expected_port
):
    result = reconstruct(domain_index, "/p")
    assert result.host == expected_host
    assert result.port == expected_port
    assert result.url == f"http://{expected_host}:{expected_port}/p"


# -- IPs: the trap is that they are NOT reversed ---------------------------


@pytest.mark.parametrize(
    ("domain_index", "expected"),
    [
        ("http://V4.66.102.9.104.", "66.102.9.104"),      # real
        ("http://V4.64.233.167.104.", "64.233.167.104"),  # real
    ],
)
def test_ipv4_is_not_reversed(domain_index, expected):
    """Reversing an IP would corrupt every IP host — 66.102.9.104 is not
    104.9.102.66."""
    result = reconstruct(domain_index, "/")
    assert result.host == expected
    assert result.is_ip is True


def test_ipv4_with_port():
    result = reconstruct("http://V4.12.100.23.254.:8080", "/x")  # real
    assert result.host == "12.100.23.254"
    assert result.port == 8080
    assert result.url == "http://12.100.23.254:8080/x"


def test_ipv6_marker_is_not_reversed():
    result = reconstruct("http://V6.2001.db8.1.", "/")
    assert result.is_ip is True
    assert result.host == "2001.db8.1"


# -- schemes ---------------------------------------------------------------


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_crawlable_schemes(scheme):
    assert reconstruct(f"{scheme}://com.example.", "/").crawlable


@pytest.mark.parametrize("scheme", ["irc", "ftp", "gopher", "telnet", "worldwind"])
def test_non_crawlable_schemes_are_parsed_but_flagged(scheme):
    """All five appear in real dump rows. They must parse (so they can be
    counted) but never be queued."""
    result = reconstruct(f"{scheme}://com.example.", "/x")
    assert result.scheme == scheme
    assert not result.crawlable


def test_scheme_is_lowercased():
    assert reconstruct("HTTP://com.example.", "/").scheme == "http"


# -- opaque schemes: `scheme:` with no `//` --------------------------------


@pytest.mark.parametrize(
    ("domain_index", "scheme"),
    [
        ("mailto:com.gmail.@LordRM", "mailto"),        # real
        ("mailto:org.wikipedia.@info-en", "mailto"),   # real
        ("news:ada.lang.comp.", "news"),               # real
    ],
)
def test_opaque_schemes_parse_as_non_crawlable(domain_index, scheme):
    """These are a known, expected category — not malformed data. Counting them
    as errors would inflate the malformed count and hide a real signal."""
    result = reconstruct(domain_index, None)
    assert result.scheme == scheme
    assert result.opaque is True
    assert not result.crawlable


def test_opaque_host_is_not_invented():
    """We do not reconstruct a mailto address — inventing what we cannot verify
    is worse than leaving it opaque."""
    assert reconstruct("mailto:com.gmail.@LordRM", None).host == ""


# -- unreverse_host directly ------------------------------------------------


@pytest.mark.parametrize(
    ("reversed_host", "expected"),
    [
        ("com.example.", "example.com"),
        ("com.example.www.", "www.example.com"),
        ("uk.co.bbc.news.", "news.bbc.co.uk"),
        ("org.wikipedia.en.", "en.wikipedia.org"),
        ("com.example", "example.com"),  # tolerate a missing trailing dot
    ],
)
def test_unreverse_host(reversed_host, expected):
    host, is_ip = unreverse_host(reversed_host)
    assert host == expected
    assert is_ip is False


# -- malformed input is refused, never guessed ------------------------------


@pytest.mark.parametrize("bad", ["", "no-scheme-here", "http://", "http://."])
def test_malformed_raises_rather_than_guessing(bad):
    with pytest.raises(ReconstructionError):
        reconstruct(bad, "/")


def test_literal_ellipsis_url_is_refused():
    """`http://...` really appears in enwiki — someone typed it into an article.
    It must be refused, not turned into some plausible-looking host."""
    with pytest.raises(ReconstructionError):
        reconstruct("http://...", "/")


def test_path_is_preserved_verbatim():
    """Query strings carry & and = and must survive untouched — normalization
    is v1.D's job, not the reconstructor's."""
    path = "/news/newsArticle.aspx?type=sportsNews&storyID=2006-01-22T21%3A53&x=False"
    assert reconstruct("http://com.reuters.today.", path).url.endswith(path)
