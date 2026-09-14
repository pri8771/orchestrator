# WHB-001 reviewed media correction — BOTS-117

The original million/billion video labeled bars whose lengths were 220/800, implying about 3.6:1 rather than the actual 1,000:1 relationship. This correction removes those data bars and presents numerical durations with an explicit **NOT A SCALE CHART** label and **1 BILLION = 1,000 × 1 MILLION**. The existing queue captions remain accurate and unchanged.

Accepted bytes: `WHB-001-million-vs-billion-seconds-2039fa4c0a3b5fe56ed1351e3b5092e42042dfc5dcfba072580dd76abe6101ec.mp4`.

SHA-256: `2039fa4c0a3b5fe56ed1351e3b5092e42042dfc5dcfba072580dd76abe6101ec`.

The asset is 12 seconds, 1080×1920, 30 fps, H.264 with a retained silent AAC track. Producer verification decoded all 360 frames and the entire audio track (all decoded samples zero), and visually inspected all six authored scene states. Independent `claude-haiku-4-5-20251001` editorial review returned PASS on the exact representative scene-2/scene-5 images plus complete scene text/math and supplied decode evidence. Root also accepted the corrected visual approach. The review inspected two images, not the full video; it does not establish perceptual playback, platform overlays or publishing authority. Exact hashes, scope, actual model/usage and review clarifications are in `WHB-001-editorial-provenance.json`.

The original asset and generator on the historical `wait-how-big-social-media` branch are preserved. Only WHB-001 changes URL in the operator queue. All other items/captions and actual state remain unchanged. Rebuilding the operator archive binds the new queue bytes; old queue-bound plans/grants are invalid. After integration, root must verify the public asset URL returns these exact bytes before applying current release controls. GitHub publishing remains disabled by the unchanged operator.

## Reproduction on macOS

The helpers reuse the original generator's branding/layout and require existing Pillow, Swift and macOS AVFoundation. They perform no installation, account access or publishing. The available Arial fallback changes font geometry from the original Linux rendering. No duplicate generated PNG set is stored in this repository.

Obtain the preserved original WHB-001 video only if rebuilding. Its source URL and required SHA-256 (`53f9389d20eda9a7bdd4dbfbfd81888ad9350b0591236d2e9afe4af5473f2c3e`) are in the provenance file. It supplies the existing silent AAC track. Verify those input bytes before reproduction.

From this directory, choose an output directory outside the checkout that does not already exist:

```sh
python3 render_whb001.py /absolute/path/to/new-output
swift -suppress-warnings encode_native.swift /absolute/path/to/new-output /absolute/path/to/original-WHB-001.mp4
swift -suppress-warnings verify_native.swift /absolute/path/to/new-output/WHB-001_million-vs-billion-seconds-repaired.mp4 /absolute/path/to/new-output WHB-001-rebuilt
```

The renderer writes six PNG scene states and a manifest; the native encoder creates H.264 then retains the input AAC track. Encoding refuses to overwrite existing outputs. The verifier decodes every frame and PCM sample and extracts six representative states. Reproduction helper graphics were checked byte-for-byte against all six original repaired scene PNGs and the cover.

MP4 container metadata can differ on a rebuild. Do not rename different bytes to the accepted hash filename: retain the approved asset above, or bind a new asset/review to a newly verified hash.

Real source association: [BOTS-117](https://priyanshchordia-1779372280524.atlassian.net/browse/BOTS-117). This change does not update Jira or claim publication.

## WHB-013..016 silent upload-ready assets — BOTS-122 / S04-01

Four additional explainers are bound as **silent** (TTS-free) hashed MP4s in this directory. Narrated Samantha variants remain local evidence only and are not the queue `media_url`.

| ID | Canonical silent asset | SHA-256 |
|---|---|---|
| WHB-013 | `WHB-013-earth-moon-gap-df5a1bebc85c537f7e4003e06d616dfaad33d642221440938a193cc940b4dafe.mp4` | `df5a1beb…b4dafe` |
| WHB-014 | `WHB-014-sun-volume-earths-ec3a5da266fa32508fe5ca27ccba95a49590d7add3ab501b3390fe33dc98187e.mp4` | `ec3a5da2…98187e` |
| WHB-015 | `WHB-015-everest-challenger-deep-990027e1999461b6e8cdc9490a4e83a7656c4712ffe546a0930243c586fc0bcf.mp4` | `990027e1…fc0bcf` |
| WHB-016 | `WHB-016-million-vs-billion-hours-78e14b2d4137f3503685e54d0e656d674720320b5605dc69a2e76bd1e3383492.mp4` | `78e14b2d…83492` |

Each is 12.0s (not 20s), 1080×1920, 30fps, H.264, `audio_tracks=0`. WHB-014 captions use ~109× diameter ratio (not stale ~100×). Queue rows WHB-000..012 are untouched except that 013–016 now reference these silent URLs. Live social posting remains a separate credentialed step (S04-02).
