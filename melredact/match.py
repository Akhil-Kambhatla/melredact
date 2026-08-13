"""Fuzzy name matching against the roster, with abstention as the default.

Auto-assign and "the right candidate to surface for review" are different
bars. Every packet gets a top candidate + score for a human to see,
regardless of whether it's confident enough to pre-fill -- a packet that
doesn't clear the auto-assign gate still needs its correct candidate
visible so a reviewer can approve/correct it, rather than starting from
nothing. Only entries that clear MIN_SCORE, beat the runner-up by
MIN_MARGIN, and are still unclaimed get auto-assigned. "Unclaimed" is
necessarily a whole-batch property (see assign_all), not a per-packet one,
since the build spec's own auto-assign rule requires knowing whether some
other packet has already claimed that roster entry.

MIN_SCORE/MIN_MARGIN are placeholders (see config.py) pending calibration
against the real ~22-packet file's score distribution.

A roster's `held_names` (see roster.py's module docstring) are scored
against a packet's handwritten name with the exact same `score_pair` used
for roster entries -- `HeldName` deliberately carries the same
`first_name`/`last_name` attributes `_variants` reads, so no special-casing
is needed here. `propose` scores both pools and `MatchProposal.
is_held_match` says whether the single best-scoring candidate overall (not
just the best *roster* candidate) is a held name. A held match is neither
a roster proposal nor grounds for a delete -- pipeline.py routes it to a
distinct "consent hold" disposition instead (known-consented, SID
unresolvable) -- and `assign_all` must never auto-assign a roster SID to a
packet whose best match is actually a held name, so it's excluded from the
ranked pool the same way an empty candidate list already is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from melredact.config import MIN_MARGIN, MIN_NAME_CHARS, MIN_SCORE
from melredact.roster import HeldName, Roster, RosterEntry


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _variants(entry: RosterEntry) -> list[str]:
    first, last = entry.first_name, entry.last_name
    return [f"{first} {last}", f"{last} {first}", last, first]


def score_pair(probe_text: str, entry: RosterEntry | HeldName) -> float:
    """Score one handwritten (OCR'd) name against one roster entry, trying
    both name orders and each name alone, so real OCR noise concentrated in
    one token (a garbled first name, a dropped surname) doesn't sink the
    whole comparison.

    The illegible-ink floor is applied to the probe *before* trying any
    variant, not after taking the max. Checking it after would let a short
    candidate variant (a bare last name) score a scrawl highly through
    substring-containment behavior in WRatio -- the same failure mode as
    bug (a) with partial_ratio, just reached through variants instead.
    """
    probe = _normalize(probe_text)
    if len(probe.replace(" ", "")) < MIN_NAME_CHARS:
        return 0.0

    best = 0.0
    for variant in _variants(entry):
        candidate = _normalize(variant)
        if not candidate:
            continue
        best = max(best, fuzz.WRatio(probe, candidate), fuzz.token_sort_ratio(probe, candidate))
    return best


@dataclass
class Candidate:
    sid: str
    score: float


@dataclass
class HeldCandidate:
    full_name: str
    score: float


@dataclass
class MatchProposal:
    packet_tag: str
    candidates: list[Candidate]  # sorted descending by score
    # Held-name candidates (see roster.py's Roster.held_names), sorted
    # descending by score the same way -- a separate list, not merged into
    # `candidates`, since a held name never has a sid to assign.
    held_candidates: list[HeldCandidate] = field(default_factory=list)

    @property
    def top(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def runner_up(self) -> Candidate | None:
        return self.candidates[1] if len(self.candidates) > 1 else None

    @property
    def margin(self) -> float:
        if self.top is None:
            return 0.0
        if self.runner_up is None:
            return self.top.score
        return self.top.score - self.runner_up.score

    @property
    def top_held(self) -> HeldCandidate | None:
        return self.held_candidates[0] if self.held_candidates else None

    @property
    def is_held_match(self) -> bool:
        """True when the single best-scoring candidate overall -- roster or
        held -- is a held name. Ties go to the held name: a held name is
        already a known-consented real student with an unresolvable SID, so
        when a roster candidate and a held candidate are equally plausible,
        guessing the roster SID is the riskier of the two wrong answers."""
        if self.top_held is None:
            return False
        if self.top is None:
            return True
        return self.top_held.score >= self.top.score


def propose_held(name_text: str, held_names: list[HeldName]) -> list[HeldCandidate]:
    """Score a packet's Name-row text against every held name, with the
    identical scorer `propose` uses for roster entries -- see the module
    docstring for why `HeldName` needs no special-casing here."""
    scored = [HeldCandidate(full_name=h.full_name, score=score_pair(name_text, h)) for h in held_names]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


def propose(packet_tag: str, name_text: str, roster: Roster) -> MatchProposal:
    """Score a packet's Name-row text against every roster entry, and
    separately against every held name (see propose_held). Always returns a
    full ranked candidate list, even when nothing clears the auto-assign
    bar -- review needs the top candidate regardless."""
    scored = [Candidate(sid=entry.sid, score=score_pair(name_text, entry)) for entry in roster]
    scored.sort(key=lambda c: c.score, reverse=True)
    held_candidates = propose_held(name_text, roster.held_names)
    return MatchProposal(packet_tag=packet_tag, candidates=scored, held_candidates=held_candidates)


def assign_all(proposals: list[MatchProposal]) -> dict[str, str | None]:
    """Auto-assign SIDs with zero human input: packet_tag -> sid or None.

    Processes (packet, top candidate) pairs in descending score order, so a
    genuine match reliably claims its roster entry before a merely-similar
    decoy for the same entry gets a chance to. No fallback to a packet's
    second-choice candidate if its top choice is already claimed by a
    higher-scoring packet -- that packet abstains and goes to human review
    rather than being auto-assigned a different guess.

    A proposal whose best overall match is a held name (`is_held_match`) is
    excluded from the ranked pool entirely, the same as one with no
    candidates at all -- auto-assign must never hand a held-consent, SID-
    unresolvable packet a roster SID just because some roster entry also
    scored reasonably well.
    """
    assignments: dict[str, str | None] = {p.packet_tag: None for p in proposals}
    claimed: set[str] = set()

    ranked = sorted(
        (p for p in proposals if p.top is not None and not p.is_held_match),
        key=lambda p: p.top.score,
        reverse=True,
    )
    for proposal in ranked:
        top = proposal.top
        assert top is not None
        if top.score < MIN_SCORE or proposal.margin < MIN_MARGIN:
            continue
        if top.sid in claimed:
            continue
        assignments[proposal.packet_tag] = top.sid
        claimed.add(top.sid)
    return assignments
