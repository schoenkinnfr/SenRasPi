"""
Sentinelle T1D — Raspberry Pi glucose display.

SPDX-License-Identifier: AGPL-3.0-only

NOT AN ALARM. This is a screen on a shelf, with no guaranteed wake, no
guaranteed network and no ability to make noise. Your pump/CGM alarms and
phone alerts remain the actual safety net. Every screen this program draws
shows how old its data is, and greys out entirely once readings go stale,
precisely so it can never quietly present an old number as a current one.
"""

__version__ = "1.0.0"
