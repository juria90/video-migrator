#!/usr/bin/env python3
"""
Rules that judge a field on a source site, and report what it should hold.

Each rule answers one question about one field and says why, in the form the
ledger records: a reason, and either a value to write or a question for a person
to settle. Nothing here edits anything — deciding is separate from applying, so
a rule can be run against a whole archive without risk.

The rules are generic; what they judge against is not. A site's own conventions
— which honorifics count as a title, how a service part is written into a title,
how far a publish date may drift — arrive as arguments, from that site's profile.
A second church on the same CMS should need a profile and nothing else.
"""
