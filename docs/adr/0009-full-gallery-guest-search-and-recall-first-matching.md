# ADR 0009: Full-gallery guest search and recall-first matching

- **Status:** Accepted
- **Date:** 2026-09-14
- **Supersedes:** The `selfie_only` capability and match-dependent authorization parts of ADR 0008
- **Implemented by:** ADR 0010 for the accepted face-model contract

## Context

The earlier plan assumed event links could reach outsiders or coworkers who should see only photos
returned by a strict face match. Follow-up interviews established a narrower pilot: the owner shares
guest links only with known relatives who may browse the complete event or sub-event granted by the
link. Photographers prefer a search that finds nearly all photos of the submitted person even when
that produces some extra results.

Treating a model result as authorization would still be structurally unsafe. A relative is a social
relationship, not a technical access control, and links can be forwarded. The capability must make
the complete authorized photo set explicit before face similarity is evaluated.

## Decision

- Remove `selfie_only` from the active product and Session 8 design.
- Every guest capability grants full browse and original-download access to either the complete
  event or exactly one active sub-event.
- Face search accepts exactly one usable face and filters only the photos already visible through
  that capability. Event and optional sub-event predicates are applied before vector distance.
- Return every photo with at least one face above the pinned threshold, deduplicate by asset, rank by
  the best matching face, and paginate without a fixed top-result cap.
- The threshold is part of the accepted model contract and is not adjustable by photographers or
  guests. Results are described as best effort and may contain extras or miss difficult faces; the
  UI suggests another clear single-person reference when needed.
- Session 6 optimizes recall subject to a noise guardrail: at least 98% same-person recall on the LFW
  holdout and no more than 0.01% false accepts per cross-identity comparison. On the i5-6200U/8 GB
  performance floor, 10,000 photos and up to 100,000 usable faces must project below 12 hours and 2
  GiB peak RSS.
- Representative wedding data remains required for throughput, detection yield, and dense-group
  behavior. The product owner accepts that recognition accuracy will initially be supported by LFW
  rather than identity-labelled wedding photos; this is a documented pilot risk.

## Consequences

Face-search mistakes affect relevance but cannot reveal an otherwise unauthorized photo. Session 8
does not need match-membership authorization records or a second guest view mode. Search-result
state may still exist for pagination, but it grants no access and can be discarded independently.

The exact capability scope, expiry, revocation, PIN, and throttling controls remain mandatory.
Reference inputs/crops remain ephemeral, no identity or cluster is created, and model/version/vector
validation still fails closed.

The higher-recall threshold may return unrelated photos. The fixed FAR guardrail, similarity
ranking, clear best-effort copy, and ability to return to the whole gallery make that an explicit
usability tradeoff rather than hidden authorization behavior.

## Rejected alternatives

- **Keep `selfie_only` for possible outsiders.** The pilot has no such recipient, and retaining the
  mode preserves a high-risk authorization path without a current user.
- **Let a face match authorize photos for relatives.** This makes model error a permission decision
  even though every intended recipient may browse the full scope.
- **Give guests a threshold control.** It makes results incompatible and can turn the filter into an
  unbounded similar-face query.
- **Use a fixed top-result count.** It necessarily drops true photos when a person appears more
  often than the cap.

## Verification

- Active plans and future implementation contain no `selfie_only` capability or match-membership
  download rule.
- A sub-event search cannot return an event-level or sibling-sub-event asset even when its vector is
  the nearest match.
- Every returned result is independently browsable and downloadable through the same capability.
- Session 6 reports recall, false accepts, throughput, memory, model artifacts/hashes/licence, and
  the accepted fixed threshold without recording faces, paths, filenames, or embeddings.
