#!/usr/bin/env python3
"""
Tests for the rules that judge a field on a source site.

The date rule below is the reason this file exists. It shipped twice in a form
that was confidently wrong — see ``test_an_isolated_typo_is_found`` and
``test_a_back_filled_run_is_not_reported`` — and both faults were caught by
reading output rather than by anything automatic, because the rule lived in a
script that could not be committed and so could not be tested.
"""

import datetime
import pathlib

import pytest

from video_migrator.corrections.apply import applicable, decide, stamp_landed
from video_migrator.corrections.fields import check_preacher, check_title
from video_migrator.corrections.timeline import cache_age_hours, find_misdated
from video_migrator.corrections.verses import check_verse, one_edit_apart
from video_migrator.ledger import read_ledger, write_ledger

ASK = "look it up"
TITLES = (" 목사", " 선교사")
PREFIXES = ("Rev.", "Pastor")


def series(start_id=1000, start="2020-01-05", count=40, step=7):
    """
    Build a run of records whose ids climb in step with their dates.

    :param start_id: Video id of the first record
    :param start: Date of the first record
    :param count: How many records to build
    :param step: Days between one record and the next
    :return: (video id, date, key) for each, as the rule expects
    """
    first = datetime.date.fromisoformat(start)
    return [(start_id + i * 100, first + datetime.timedelta(days=i * step), str(i)) for i in range(count)]


class TestVerses:
    """The bible reference rule."""

    def test_a_reference_that_is_correct_is_left_alone(self):
        """A well-formed reference produces no finding at all."""
        assert check_verse("요한복음 3:16", ASK) is None

    @pytest.mark.parametrize("empty", ["", ".", "-", "?"])
    def test_an_unfilled_field_is_a_question_not_a_guess(self, empty):
        """
        Nothing can be inferred from an empty field, so a person is asked.

        :param empty: A value meaning nobody filled the field in
        """
        assert check_verse(empty, ASK) == ("missing", ASK)

    def test_a_typo_one_letter_from_one_book_is_corrected(self):
        """The books being a closed set is what makes this settleable without a lookup."""
        reason, fixed = check_verse("요한복은 3:16", ASK)
        assert fixed == "요한복음 3:16"
        assert "요한복음" in reason

    def test_a_typo_between_two_books_is_refused(self):
        """
        Where two books are equally close, the differing character is the one
        that would have to be guessed — so the rule asks instead.
        """
        reason, suggestion = check_verse("사무엘장 3:1", ASK)
        assert suggestion == ASK, "must not choose between 사무엘상 and 사무엘하"
        assert "not a book" in reason

    def test_a_typo_two_letters_out_is_refused(self):
        """One edit is the reach; beyond it the rule does not speculate."""
        assert check_verse("예배소서 2:14", ASK) == ("'예배소서' is not a book of the bible", ASK)

    def test_a_space_inside_a_book_name_is_closed_up(self):
        """A real book name with a space typed into it needs no lookup."""
        assert check_verse("요한 복음 3:16", ASK) == ("space inside the book name", "요한복음 3:16")

    def test_a_semicolon_between_digits_is_a_mistyped_colon(self):
        """A semicolon between two digits is a slipped keystroke."""
        assert check_verse("요한복음 3;16", ASK) == ("semicolon where a colon belongs", "요한복음 3:16")

    def test_a_semicolon_joining_passages_is_left_alone(self):
        """
        A semicolon between two references is how passages are joined.

        Correcting it would silently merge two readings into one, which is why
        the rule looks for digits on both sides rather than for the character.
        """
        assert check_verse("요한복음 3:16; 로마서 8:1", ASK) is None

    def test_a_chapter_run_on_against_the_book_is_spaced(self):
        """The book and the chapter are two things, and the site writes them as one."""
        assert check_verse("마가복음9:1-8", ASK) == ("no space between the book and the chapter", "마가복음 9:1-8")

    def test_a_space_is_added_after_the_book_name_is_closed_up(self):
        """
        Closing up a space inside the name moves where the name ends.

        Both defects appear together — the space was typed in the wrong place
        rather than merely omitted — so the second rule has to read the first
        one's output, not the value the site holds.
        """
        assert check_verse("사무엘 상1:26-2:3", ASK) == (
            "space inside the book name; no space between the book and the chapter",
            "사무엘상 1:26-2:3",
        )

    @pytest.mark.parametrize(("spelled", "figures"), [
        ("출애굽기 2장 1절에서 10절", "출애굽기 2:1-10"),
        ("출애굽기 17장 8절부터 16절", "출애굽기 17:8-16"),
        ("사무엘상 1장 26절에서 2장 3절", "사무엘상 1:26-2:3"),
        ("요한복음 3장 16절", "요한복음 3:16"),
        ("예레미야 23장 9절, 29절", "예레미야 23:9, 29"),
        ("다니엘 12장 3절로 4절", "다니엘 12:3-4"),
        # Either 절 may be left implied, on the range's end or on its start.
        ("민수기 3장 11절-13", "민수기 3:11-13"),
        ("마가복음 9장 14-29절", "마가복음 9:14-29"),
    ])
    def test_a_reference_spelled_out_is_written_in_figures(self, spelled, figures):
        """
        The board's own style is ``3:16``, and 장/절 says the same thing at length.

        :param spelled: The reference as the site stores it
        :param figures: The same reference in the style the board keeps
        """
        assert check_verse(spelled, ASK) == ("chapter and verse spelled out", figures)

    @pytest.mark.parametrize(("wrapped", "bare"), [
        ("<요한복음 6장 15절에서 21절>", "요한복음 6:15-21"),
        ("(누가복음 23장 43절)", "누가복음 23:43"),
    ])
    def test_punctuation_wrapped_round_a_reference_is_taken_off(self, wrapped, bare):
        """
        The board's style has no brackets round a reading.

        :param wrapped: The reference as the site stores it
        :param bare: The reading on its own
        """
        reason, fixed = check_verse(wrapped, ASK)
        assert fixed == bare
        assert reason.startswith("punctuation wrapped round the reference")

    @pytest.mark.parametrize(("bracketed", "bare"), [
        ("(John) 12:12-16", "John 12:12-16"),
        ("[요한복음] 3:16", "요한복음 3:16"),
    ])
    def test_brackets_round_the_book_name_come_off(self, bracketed, bare):
        """
        The brackets say nothing the reference does not, and the board has none.

        The name comes out of them rather than the opening bracket being struck
        off on its own — that leaves ``John) 12:12-16``, which is worse than
        what it replaced and which no later rule can see is wrong.

        :param bracketed: The reference as the site stores it
        :param bare: The reading with its book name unbracketed
        """
        assert check_verse(bracketed, ASK) == ("brackets round the book name", bare)

    def test_a_reference_that_types_a_chapter_for_a_verse_is_left_whole(self):
        """
        ``3장 1장에서 10장`` says 장 where it means 절, three times over.

        Read as far as it parses it becomes ``3:1장에서 10장`` — a value worse
        than the one it replaced, because half of it now looks deliberate. A
        verse number must say 절 or say nothing, never name a chapter.
        """
        assert check_verse("출애굽기 3장 1장에서 10장", ASK) is None

    def test_a_hyphen_used_as_a_wrapper_is_not_read_as_a_range(self):
        """
        A hyphen opens a reference as often as an angle bracket does here.

        Each end is stripped on its own rather than as a matched pair, because
        the pair is usually mismatched — and an abbreviated book name survives
        it, being no business of this rule's.
        """
        reason, fixed = check_verse("-요 11장 38절에서 44절-", ASK)
        assert fixed == "요 11:38-44"
        assert reason.startswith("punctuation wrapped round the reference")

    def test_a_tilde_between_verses_is_the_range_hyphen(self):
        """The board writes a range with a hyphen; the tilde is the same thing typed."""
        assert check_verse("요한복음 3:16~18", ASK) == ("tilde where a hyphen belongs", "요한복음 3:16-18")

    def test_a_chapter_alone_is_left_spelled_out(self):
        """
        ``23편`` and ``3장`` name a whole chapter, which ``3:`` cannot express.

        Rewriting one would have to invent a verse, so the rule only fires where
        a verse was written out to convert.
        """
        assert check_verse("시편 23편", ASK) is None
        assert check_verse("요한복음 3장", ASK) is None

    def test_a_range_of_chapters_is_not_read_as_verses(self):
        """
        ``15-16장`` is two chapters, and ``15:16`` would be one verse of one.

        The numbers sit on the wrong side of 장 for the rule to reach, which is
        what keeps a chapter range from being rewritten into a reading nobody
        gave.
        """
        assert check_verse("이사야 15-16장", ASK) is None

    def test_every_defect_in_one_reference_is_reported_at_once(self):
        """
        A row fixed one defect per pass costs an apply-and-rescrape round each.

        :meta note: 마가복음9;1-8 is a run-on book, a mistyped colon, and would
            still be wrong after either fix alone.
        """
        reason, fixed = check_verse("마가복음9;1-8", ASK)
        assert fixed == "마가복음 9:1-8"
        assert reason == "semicolon where a colon belongs; no space between the book and the chapter"

    def test_a_correction_that_would_still_be_wrong_is_not_offered(self):
        """
        Fixing the book but leaving no chapter would raise the same row again
        next run, so the rule asks rather than proposing a value it will reject.
        """
        assert check_verse("요한복은", ASK) == ("'요한복은' is not a book of the bible", ASK)

    @pytest.mark.parametrize(("spelled", "book", "near"), [
        ("요한복은", "요한복음", True),
        ("요한복음서", "요한복음", True),
        ("한복음", "요한복음", True),
        ("요한복음", "요한복음", False),
        ("요한계시록", "요한복음", False),
    ])
    def test_one_edit_apart(self, spelled, book, near):
        """
        A single substitution, insertion or deletion counts; more does not.

        :param spelled: The name as written
        :param book: The book to compare against
        :param near: Whether they should be judged one edit apart
        """
        assert one_edit_apart(spelled, book) is near


class TestPreacher:
    """The preacher rule."""

    def test_a_name_with_a_title_is_accepted(self):
        """An honorific is the whole test."""
        assert check_preacher("홍길동 목사", ASK, TITLES, PREFIXES) is None

    def test_a_name_with_a_prefix_is_accepted(self):
        """Some names carry the honorific in front instead."""
        assert check_preacher("Rev. John Doe", ASK, TITLES, PREFIXES) is None

    def test_an_affiliation_does_not_hide_the_title(self):
        """A bracketed affiliation is not part of the name."""
        assert check_preacher("김영희 선교사 (예시교회)", ASK, TITLES, PREFIXES) is None

    def test_a_bare_name_is_questioned(self):
        """Without an honorific the field may hold something that is not a name."""
        assert check_preacher("홍길동", ASK, TITLES, PREFIXES) == ("name carries no title", ASK)

    def test_the_accepted_titles_come_from_the_profile(self):
        """
        A church's honorifics are its own. The same name is accepted or not
        depending only on what its profile lists, with no default to fall back on.
        """
        assert check_preacher("홍길동 장로", ASK, TITLES, PREFIXES) is not None
        assert check_preacher("홍길동 장로", ASK, (" 장로",), PREFIXES) is None


class TestTitle:
    """The title rule."""

    def test_a_title_in_the_agreed_form_is_left_alone(self):
        """The form the church settled on produces no finding."""
        assert check_title("설교 제목 (1부)", "홍길동 목사") is None

    @pytest.mark.parametrize("written", [
        "1부 - 설교 제목",
        "1부 설교 제목",
        "(1부)설교 제목",
        "(1부예배) 설교 제목",
        "설교 제목 1부",
    ])
    def test_a_service_part_is_settled_into_one_form(self, written):
        """
        However the part was typed, it ends up in one place.

        :param written: A form the part has been written in
        """
        assert check_title(written, "홍길동 목사") == ("service part not in the agreed form", "설교 제목 (1부)")

    def test_a_part_left_in_the_preacher_field_is_moved(self):
        """The part belongs to the recording, not to whoever preached."""
        assert check_title("설교 제목", "홍길동 목사 2부설교") == (
            "service part is in the preacher field", "설교 제목 (2부)")

    def test_a_part_already_in_the_title_is_not_doubled(self):
        """A part named in both fields is not appended a second time."""
        assert check_title("설교 제목 (1부)", "홍길동 목사 1부설교") is None

    def test_a_doubled_space_is_closed_up(self):
        """Two spaces where one belongs."""
        assert check_title("설교  제목", "홍길동 목사") == ("double space", "설교 제목")

    def test_the_agreed_form_comes_from_the_profile(self):
        """
        How a part is written into a title is a decision a church made, so it
        arrives as an argument rather than being fixed here.
        """
        assert check_title("1부 - 설교 제목", "홍길동 목사", "{part} {title}") == (
            "service part not in the agreed form", "1부 설교 제목")


class TestMisdated:
    """The rule that reads a date against the order its video was uploaded in."""

    def test_a_clean_run_reports_nothing(self):
        """Ids climbing in step with dates is the ordinary case."""
        assert find_misdated(series()) == {}

    def test_an_isolated_typo_is_found(self):
        """
        One wrong date among correct ones is exactly what this exists to catch.

        It once did not. Estimating from the immediately neighbouring records
        made the typo an endpoint of its own neighbours' brackets, so all three
        looked wrong together and a cluster test then dismissed all three.
        """
        records = series()
        wrong = records[20]
        records[20] = (wrong[0], wrong[1].replace(year=wrong[1].year - 1), wrong[2])

        found = find_misdated(records)
        assert set(found) == {wrong[2]}
        assert abs((found[wrong[2]] - wrong[1]).days) <= 3, "the estimate should land near the true date"

    def test_a_back_filled_run_is_not_reported(self):
        """
        An archive uploaded later, in reverse, is correctly dated throughout.

        Every record in such a run sits far off the curve, and reporting them
        would mean re-examining correct records on every run forever. What
        distinguishes them is that their neighbourhood is out of order too.
        """
        records = series()
        backfill = [(500 + i * 10, datetime.date(2015, 6, 28) - datetime.timedelta(days=i * 7), f"b{i}")
                    for i in range(12)]
        assert find_misdated(backfill + records) == {}

    def test_the_neighbours_of_a_typo_are_not_blamed_for_it(self):
        """A wrong date must not drag correct records into being reported."""
        records = series()
        wrong = records[15]
        records[15] = (wrong[0], wrong[1].replace(year=wrong[1].year + 2), wrong[2])

        assert set(find_misdated(records)) == {wrong[2]}

    def test_a_drift_inside_the_tolerance_is_ignored(self):
        """A date a few days out is a late upload, not a typo."""
        records = series()
        near = records[20]
        records[20] = (near[0], near[1] + datetime.timedelta(days=10), near[2])

        assert find_misdated(records) == {}

    def test_too_few_records_to_judge(self):
        """With nothing to compare against, nothing is claimed."""
        assert find_misdated(series(count=3)) == {}


class TestCacheAge:
    """The freshness check that guards every conclusion drawn from a scrape."""

    def test_an_empty_cache_has_no_age(self, tmp_path):
        """
        Nothing scraped yet is not the same as scraped long ago.

        :param tmp_path: An empty directory
        """
        assert cache_age_hours(tmp_path) is None

    def test_a_fresh_cache_is_hours_old_at_most(self, tmp_path):
        """
        A file just written reads as new.

        :param tmp_path: A directory to write a cached file into
        """
        (tmp_path / "record.html").write_text("x", encoding="utf-8")
        assert 0 <= cache_age_hours(tmp_path) < 1

    def test_age_comes_from_the_newest_file(self, tmp_path):
        """
        One old file among fresh ones does not make the scrape old.

        :param tmp_path: A directory to write cached files into
        """
        old = tmp_path / "old.html"
        old.write_text("x", encoding="utf-8")
        import os
        long_ago = datetime.datetime.now().timestamp() - 60 * 60 * 24 * 30
        os.utime(old, (long_ago, long_ago))
        (tmp_path / "new.html").write_text("x", encoding="utf-8")

        assert cache_age_hours(tmp_path) < 1


class TestApply:
    """The decisions made before anything is written back to a site."""

    INPUTS = {"title": "subject", "verse": "word"}

    def test_a_record_untouched_since_the_scan_is_written(self):
        """The only safe case: the site still holds what the scan recorded."""
        assert decide("설교 제목", "설교 제목", "설교 제목 (1부)") == "write"

    def test_a_record_already_correct_is_skipped(self):
        """
        Already holding the target value is what applied means, however it got
        there — so re-running after an interruption costs nothing.
        """
        assert decide("설교 제목 (1부)", "설교 제목", "설교 제목 (1부)") == "skip"

    def test_a_record_edited_since_the_scan_is_left_alone(self):
        """
        Holding neither value means someone has been here since, and their
        version outranks a suggestion made against a stale snapshot.
        """
        assert decide("설교 제목 (2부)", "설교 제목", "설교 제목 (1부)") == "diverged"

    def test_a_question_is_never_written(self):
        """A suggestion in parentheses is addressed to a person, not to the site."""
        row = {"updated_at": "", "field": "verse", "new": "(look it up)"}
        assert applicable(row, self.INPUTS) is None

    def test_a_recovered_answer_applies_like_any_other(self):
        """
        A field that was missing and has since been filled in by hand is
        ordinary work. Gating on the reason rather than the value once blocked
        every recovered answer from ever being applied.
        """
        row = {"updated_at": "", "field": "verse", "new": "요한복음 3:16", "reason": "missing"}
        assert applicable(row, self.INPUTS) == "word"

    def test_a_value_that_merely_opens_with_a_bracket_is_still_a_value(self):
        """
        A question is wrapped in parentheses; a reference may only begin with one.

        Reading ``(John) 12:12-16`` as a note left the two records holding it
        with no way to be written back at all — the repair sat in the ledger
        being silently skipped every round.
        """
        row = {"updated_at": "", "field": "verse", "new": "(John) 12:12-16"}
        assert applicable(row, self.INPUTS) == "word"

    def test_history_is_not_work(self):
        """A stamped row has already been applied."""
        row = {"updated_at": "2024-01-01 12:00", "field": "verse", "new": "요한복음 3:16"}
        assert applicable(row, self.INPUTS) is None

    def test_a_field_with_nowhere_to_write_it(self):
        """A date has no form input here, so it is reported rather than applied."""
        row = {"updated_at": "", "field": "date", "new": "2024-01-07"}
        assert applicable(row, self.INPUTS) is None

    def test_kinds_restrict_what_is_offered(self):
        """One kind at a time is how a batch is kept reviewable."""
        row = {"updated_at": "", "field": "title", "new": "설교 제목 (1부)"}
        assert applicable(row, self.INPUTS, {"verse"}) is None
        assert applicable(row, self.INPUTS, {"title"}) == "subject"

    def test_stamping_marks_only_what_landed(self, tmp_path):
        """
        A stamp says a change was observed on the site, so it is written only
        for the pairs that were verified, and never over an existing one.

        :param tmp_path: Directory to write a ledger into
        """
        path = pathlib.Path(tmp_path) / "ledger.tsv"
        columns = ["num", "field", "old", "new", "reason", "source", "updated_at"]
        rows = [
            {"num": "1", "field": "title", "old": "a", "new": "b", "reason": "", "source": "scan rule",
             "updated_at": ""},
            {"num": "2", "field": "title", "old": "a", "new": "b", "reason": "", "source": "scan rule",
             "updated_at": ""},
            {"num": "3", "field": "title", "old": "a", "new": "b", "reason": "", "source": "scan rule",
             "updated_at": "2020-01-01 00:00"},
        ]
        write_ledger(path, rows, columns)

        assert stamp_landed(path, {("1", "title"), ("3", "title")}, "2024-06-01 09:00") == 1
        after = {r["num"]: r["updated_at"] for r in read_ledger(path)}
        assert after["1"] == "2024-06-01 09:00", "the verified row is stamped"
        assert after["2"] == "", "an untouched row is left outstanding"
        assert after["3"] == "2020-01-01 00:00", "an existing stamp is not overwritten"
