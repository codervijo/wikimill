"""The streaming MySQL INSERT parser.

Fixtures mirror the real dump's shape (sampled 2026-07-25): batched tuples on
one very long line, backslash-escaped quotes inside values, and blob columns
that may be NULL.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from wikimill.errors import DumpError
from wikimill.wiki.dump_sql import (
    dump_run_from_name,
    iter_rows,
    lang_from_name,
    parse_tuples,
)

# Verbatim from enwiki-20260701-externallinks.sql.gz.
REAL_LINE = (
    b"INSERT INTO `externallinks` VALUES "
    b"(2,3850540,'http://edu.berkeley.housing.www.','/housing/'),"
    b"(3,3850540,'http://com.freebornhall.www.','/History/ResidenceHalls/'),"
    b"(39,3260893,'http://uk.co.bbc.news.','/sport1/hi/football/fa_cup/results/default.stm');"
)


def test_parses_real_line():
    rows = list(parse_tuples(REAL_LINE[REAL_LINE.index(b"VALUES ") + 7 :]))
    assert len(rows) == 3
    assert rows[0] == (2, 3850540, "http://edu.berkeley.housing.www.", "/housing/")
    assert rows[2][1] == 3260893


def test_escaped_quote_does_not_split_the_value():
    """Real data contains `/wiki/Stating_the_bleedin\\'_obvious`. Splitting on
    `'` would truncate it and shift every following field."""
    payload = rb"(1,2,'http://org.example.','/wiki/Stating_the_bleedin\'_obvious');"
    (row,) = list(parse_tuples(payload))
    assert row[3] == "/wiki/Stating_the_bleedin'_obvious"


def test_escaped_backslash_before_quote():
    payload = rb"(1,2,'http://org.example.','/a\\');"
    (row,) = list(parse_tuples(payload))
    assert row[3] == "/a\\"


def test_null_path_becomes_none():
    """el_to_path is `blob DEFAULT NULL` — NULL means no path, not an error."""
    (row,) = list(parse_tuples(b"(1,2,'http://com.example.',NULL);"))
    assert row[3] is None


def test_commas_inside_a_value_are_not_field_separators():
    payload = rb"(1,2,'http://com.example.','/a?x=1,2,3&y=4');"
    (row,) = list(parse_tuples(payload))
    assert row[3] == "/a?x=1,2,3&y=4"


def test_parens_inside_a_value_do_not_end_the_tuple():
    payload = rb"(1,2,'http://org.example.','/wiki/Foo_(disambiguation)');"
    (row,) = list(parse_tuples(payload))
    assert row[3] == "/wiki/Foo_(disambiguation)"


def test_escape_sequences_decoded():
    payload = rb"(1,2,'http://com.example.','/a\nb\tc');"
    (row,) = list(parse_tuples(payload))
    assert row[3] == "/a\nb\tc"


def test_stops_at_statement_terminator():
    payload = b"(1,2,'http://com.example.','/a');\n-- trailing junk (9,9,'x','y')"
    assert len(list(parse_tuples(payload))) == 1


# -- whole-file streaming ---------------------------------------------------


def write_gz(path: Path, body: bytes) -> Path:
    with gzip.open(path, "wb") as fh:
        fh.write(body)
    return path


HEADER = (
    b"-- MySQL dump\n"
    b"DROP TABLE IF EXISTS `externallinks`;\n"
    b"CREATE TABLE `externallinks` (`el_id` int, `el_from` int);\n"
)


def test_iter_rows_skips_ddl_and_reads_data(tmp_path):
    dump = write_gz(tmp_path / "enwiki-20260701-externallinks.sql.gz",
                    HEADER + REAL_LINE + b"\n")
    rows = list(iter_rows(dump))
    assert len(rows) == 3
    assert rows[0][0] == 2


def test_iter_rows_ignores_other_tables(tmp_path):
    body = (
        b"INSERT INTO `page` VALUES (1,'Nope');\n" + REAL_LINE + b"\n"
    )
    dump = write_gz(tmp_path / "enwiki-20260701-externallinks.sql.gz", body)
    assert len(list(iter_rows(dump))) == 3


def test_iter_rows_handles_statement_without_trailing_newline(tmp_path):
    dump = write_gz(tmp_path / "enwiki-20260701-externallinks.sql.gz",
                    HEADER + REAL_LINE)
    assert len(list(iter_rows(dump))) == 3


def test_iter_rows_handles_many_batched_statements(tmp_path):
    """Real statements hold thousands of tuples across ~1 MB lines."""
    tuples = b",".join(
        b"(%d,%d,'http://com.example.','/p%d')" % (i, i, i) for i in range(5000)
    )
    body = HEADER + b"INSERT INTO `externallinks` VALUES " + tuples + b";\n"
    dump = write_gz(tmp_path / "enwiki-20260701-externallinks.sql.gz", body)
    rows = list(iter_rows(dump))
    assert len(rows) == 5000
    assert rows[-1][3] == "/p4999"


def test_missing_dump_is_a_typed_error(tmp_path):
    with pytest.raises(DumpError) as exc:
        list(iter_rows(tmp_path / "absent.sql.gz"))
    assert exc.value.remediation


def test_invalid_utf8_is_replaced_not_fatal(tmp_path):
    """Columns are binary blobs; one bad byte must not abort a 4.9 GB pass."""
    body = HEADER + b"INSERT INTO `externallinks` VALUES (1,2,'http://com.example.','/\xff\xfe');\n"
    dump = write_gz(tmp_path / "enwiki-20260701-externallinks.sql.gz", body)
    rows = list(iter_rows(dump))
    assert len(rows) == 1
    assert isinstance(rows[0][3], str)


# -- filename metadata ------------------------------------------------------


def test_dump_run_extracted():
    assert dump_run_from_name(Path("enwiki-20260701-externallinks.sql.gz")) == "20260701"


def test_dump_run_absent():
    assert dump_run_from_name(Path("externallinks.sql.gz")) is None


@pytest.mark.parametrize(
    ("name", "lang"),
    [
        ("enwiki-20260701-externallinks.sql.gz", "en"),
        ("dewiki-20260701-externallinks.sql.gz", "de"),
        # Not all language codes are 2-3 letters — a fixed-width match would
        # silently mislabel these.
        ("simplewiki-20260701-externallinks.sql.gz", "simple"),
        ("zh_yuewiki-20260701-externallinks.sql.gz", "zh_yue"),
        ("weird.sql.gz", "en"),
    ],
)
def test_lang_from_name(name, lang):
    assert lang_from_name(Path(name)) == lang
