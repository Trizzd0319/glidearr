"""
MDBList service (opt-in)
========================
MDBList (mdblist.com) aggregates ratings (IMDb / TMDb / Trakt / Letterboxd / RT /
Metacritic / MAL) and hosts curated/dynamic lists in ONE API. This package is the
FETCH/CACHE adapter for it.

First slice: AUTH + account-TIER validation only (``client.validate_key``) — the
integration is OPT-IN, so with no ``mdblist.apikey`` configured nothing runs and
the rest of the system is byte-identical. Candidate-gathering + rating-enrichment
build on this foundation later.
"""
