# 2026-08-12 summary

## Snapshot

- Captured 1 memory event.
- Main work: Voice gained a first-class surface shaped like the existing media path. The client pool learned a voice entry keyed once per process, because the library's voice factory takes no arguments and reads AI_VOICE_ENGINE itself, unlike the engine-and-model keying every other capability needs. Synthesis returns a stored artifact reference so audio is fetched with a progress bar and a resumable range, matching how generated images and video are delivered instead of inlining bytes into a JSON body. The catalogue endpoint reports voices, output formats, and engine capabilities read from the library's own published metadata rather than a table maintained in this repo, so switching engines reports the truth without a code change. Requests naming SSML or emotion direction are refused up front when the configured engine publishes no support, rather than being sent, silently stripped, and billed. Voice and format lists are bounded, since one provider publishes over two thousand voices and an unfiltered response is a payload rather than a menu.
- Top decision: None.
- Blockers: docs/requirements.md lines 67-75 name voice as unexposed rather than blocked and state the missing piece is the work. The change spans clients.py, routes_v1.py, and schemas.py (417 insertions, 1 deletion, uncommitted on feat/voice-tts). The artifact reference and ranged-content plumbing it reuses is already in routes_v1.py and covered by tests/test_artifacts_and_media.py, and the delivery pattern was established by commit e2bcf90 for generated images and video. ADR-0002 constrains this to the published library's public API with no service-side workarounds.

| Metric | Value |
|---|---|
| Memory events captured | 1 |
| Repo files changed | 1 |
| Decision candidates | 0 |
| Active blockers | 1 |

## Major work completed

- Voice gained a first-class surface shaped like the existing media path. The client pool learned a voice entry keyed once per process, because the library's voice factory takes no arguments and reads AI_VOICE_ENGINE itself, unlike the engine-and-model keying every other capability needs. Synthesis returns a stored artifact reference so audio is fetched with a progress bar and a resumable range, matching how generated images and video are delivered instead of inlining bytes into a JSON body. The catalogue endpoint reports voices, output formats, and engine capabilities read from the library's own published metadata rather than a table maintained in this repo, so switching engines reports the truth without a code change. Requests naming SSML or emotion direction are refused up front when the configured engine publishes no support, rather than being sent, silently stripped, and billed. Voice and format lists are bounded, since one provider publishes over two thousand voices and an unfiltered response is a payload rather than a menu.

## Why this mattered

- The service exists to expose the pinned library through HTTP without changing the library (ADR-0001, ADR-0002), and voice was the one published capability with no route in front of it. The requirements doc had already retracted an earlier deferral, recording that the storage question was settled and only the implementation was outstanding. This checkpoint marks that gap being closed on the same terms the rest of the service uses, so a future contributor reading the out-of-scope list knows it is stale for voice.

## Active blockers

- docs/requirements.md lines 67-75 name voice as unexposed rather than blocked and state the missing piece is the work. The change spans clients.py, routes_v1.py, and schemas.py (417 insertions, 1 deletion, uncommitted on feat/voice-tts). The artifact reference and ranged-content plumbing it reuses is already in routes_v1.py and covered by tests/test_artifacts_and_media.py, and the delivery pattern was established by commit e2bcf90 for generated images and video. ADR-0002 constrains this to the published library's public API with no service-side workarounds.

## Decision candidates

- None

## Next likely steps

- No voice tests exist yet; tests/ has no voice module, so the catalogue fallbacks, the capability refusals, and the artifact round-trip are unexercised.
- The work is uncommitted on feat/voice-tts and has not been through review or a version bump.
- docs/requirements.md still lists voice under 'Out of scope for v1' and will contradict the shipped surface once this lands.
- Speech to text is part of the library's voice surface and is reported by the capabilities model but has no route, so the exposure is partial.

## Relevant event shards

- [2026-08-12 16:06:16 UTC by 2355287-davecthomas](events/2026-08-12T16-06-16Z--2355287-davecthomas--thread_d27594f4-dbc0-4c77-a8d1-40b345330ae1--turn_64166a5b4d.md)
