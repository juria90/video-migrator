#!/usr/bin/env python3
"""
Multi-Pattern Regex Replacement Utility

This module provides functionality to perform multiple regex-based string replacements
in a single pass, improving efficiency when multiple patterns need to be replaced.
Supports both static replacement strings and dynamic replacement functions.
"""

import re
from collections.abc import Callable
from re import Pattern


def multi_replace(text: str, replacements: dict[str, str | Callable[[re.Match], str]]) -> str:
    """
    Perform multiple regex replacements in a single re.sub call.

    :param text: The input string to perform replacements on
    :param replacements: Dictionary mapping regex patterns to replacement strings or replacement functions
    :return: The text with all replacements applied

    >>> replacements = {
    ...     r'\\bcat\\b': 'CAT',
    ...     r'\\bdog\\b': 'DOG',
    ...     r'\\d+': lambda m: f'[{int(m.group()) * 2}]',
    ...     r'Hello': 'Greetings'
    ... }
    >>> multi_replace("Hello World! cat sat on 42 mats. dog ran 100 times.", replacements)
    'Greetings World! CAT sat on [84] mats. DOG ran [200] times.'

    >>> test_text2 = "John Smith and Jane Doe"
    >>> replacements2 = {
    ...     r'(\\w+) (\\w+)': r'\\2, \\1'
    ... }
    >>> multi_replace(test_text2, replacements2)
    'Smith, John Jane, and Doe'
    """
    # Combine all patterns into one with named groups
    combined_pattern = "|".join(f"(?P<g{i}>{pattern})" for i, pattern in enumerate(replacements.keys()))

    # Create a compiled regex
    regex: Pattern[str] = re.compile(combined_pattern)

    # Create replacement function
    def replace_func(match: re.Match) -> str:
        # Find which group matched
        for i, (pattern, replacement) in enumerate(replacements.items()):
            group_name = f"g{i}"
            if match.group(group_name) is not None:
                # If replacement is a function, call it with the match
                if callable(replacement):
                    # Create a new match object for just this pattern
                    inner_match = re.match(pattern, match.group(group_name))
                    return replacement(inner_match) if inner_match else replacement(match)
                # Otherwise, use it as a string (supports backreferences)
                else:
                    # Re-match with the original pattern to get proper groups for backreferences
                    inner_match = re.match(pattern, match.group(group_name))
                    if inner_match:
                        return inner_match.expand(replacement)
                    return replacement
        return match.group(0)

    # Perform single substitution with callback
    return regex.sub(replace_func, text)
