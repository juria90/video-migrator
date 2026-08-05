#!/usr/bin/env python3

import pytest

from video_migrator.utils.multi_regex_replace import multi_replace


class TestMultiReplace:
    """Test suite for multi_replace function."""

    def test_simple_string_replacements(self) -> None:
        """Test basic string replacements."""
        text = "Hello World! cat sat on mat."
        replacements = {r"\bcat\b": "dog", r"\bmat\b": "floor", r"Hello": "Goodbye"}
        result = multi_replace(text, replacements)
        assert result == "Goodbye World! dog sat on floor."

    def test_numeric_replacements_with_lambda(self) -> None:
        """Test replacements using lambda functions."""
        text = "I have 5 apples and 10 oranges."
        replacements = {r"\d+": lambda m: str(int(m.group()) * 2)}
        result = multi_replace(text, replacements)
        assert result == "I have 10 apples and 20 oranges."

    def test_backreference_replacements(self) -> None:
        """Test replacements with regex backreferences."""
        text = "John Smith; Jane Doe"
        replacements = {r"(\w+) (\w+)": r"\2, \1"}
        result = multi_replace(text, replacements)
        assert result == "Smith, John; Doe, Jane"

    def test_multiple_pattern_types(self) -> None:
        """Test mixing string replacements and lambda functions."""
        text = "cat 42 dog 100"
        replacements = {r"\bcat\b": "CAT", r"\bdog\b": "DOG", r"\d+": lambda m: f"[{int(m.group()) * 2}]"}
        result = multi_replace(text, replacements)
        assert result == "CAT [84] DOG [200]"

    def test_no_matches(self) -> None:
        """Test when no patterns match."""
        text = "Hello World"
        replacements = {r"\bxyz\b": "abc", r"\d+": "0"}
        result = multi_replace(text, replacements)
        assert result == "Hello World"

    def test_empty_string(self) -> None:
        """Test with empty input string."""
        text = ""
        replacements = {r"\bcat\b": "dog"}
        result = multi_replace(text, replacements)
        assert result == ""

    def test_empty_replacements(self) -> None:
        """Test with empty replacements dictionary."""
        text = "Hello World"
        replacements = {}
        result = multi_replace(text, replacements)
        assert result == "Hello World"

    def test_overlapping_patterns(self) -> None:
        """Test behavior with potentially overlapping patterns."""
        text = "hello hello world"
        replacements = {r"hello": "hi", r"hello world": "greetings"}
        result = multi_replace(text, replacements)
        # First pattern to match wins
        assert result == "hi hi world"

    def test_case_sensitive_matching(self) -> None:
        """Test that matching is case-sensitive by default."""
        text = "Cat cat CAT"
        replacements = {r"\bcat\b": "dog"}
        result = multi_replace(text, replacements)
        assert result == "Cat dog CAT"

    def test_special_characters_in_replacement(self) -> None:
        """Test replacements containing special characters."""
        text = "price: $10"
        replacements = {r"\$(\d+)": r"USD \1.00"}
        result = multi_replace(text, replacements)
        assert result == "price: USD 10.00"

    def test_lambda_with_match_object(self) -> None:
        """Test lambda function that uses the full match object."""
        text = "abc123def456"
        replacements = {r"[a-z]+": lambda m: m.group().upper(), r"\d+": lambda m: f"({m.group()})"}
        result = multi_replace(text, replacements)
        assert result == "ABC(123)DEF(456)"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
