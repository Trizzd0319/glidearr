"""scoring — the 0-100 watchability engines.

Pure functions: score_movie + score_show (both importing the shared constants and
helpers from ``_shared.py`` so neither reads the other) and the critic blend. This
is the crown jewel pulled out of trakt/*/scorer.py.
"""
