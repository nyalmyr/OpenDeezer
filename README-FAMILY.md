# OpenDeezer Home & Famille 0.3.0

Community extension for **Music Assistant 2.10.9** and Home Assistant, built on [OpenDeezer](https://github.com/Cycl0o0/OpenDeezer). Experimental; not an official Music Assistant or Deezer integration. AGPL-3.0, preserving the upstream license and attribution.

## What it does

- One private Deezer profile per household member, with separate personal libraries.
- Weighted household mixes: choose participants, their relative weights, an approximate duration and two successive moods. Shared artists, explicit likes and artist diversity influence the selection; tracks are deduplicated. Every selection explains its choices.
- Local like / dislike / ban / clear-feedback preferences. A ban by any participant vetoes that track in new mixes.
- A French management page at **`/opendeezer-family/`**, authenticated by Music Assistant's own login. Saved mixes, account reconnection/removal, sanitized diagnostics.
- Native Music Assistant playback, queue transfer and compatible speaker grouping. Resume the previous personal queue if still identifiable, otherwise restart the saved selection.
- Native Music Assistant **Party** mode: temporary guest access, track proposals and boosts, rate limits and duplicate prevention. Guest access stays off until explicitly opened.
- Bounded metadata caching; timing diagnostics distinguish metadata preparation from an audible playback measurement.
- Optional **Chrome/Edge companion** to send the logged-in Deezer ARL to your own server without copying it. Explicit pairing, ten-minute ticket, single use. See [installation](browser-companion/README.md). The unpacked extension is not published in a browser store; real browser enrollment still needs validation.
- Six Home Assistant scripts for profiles, mixed playback, personal resume, feedback, explanation and queue transfer. No paid LLM calls, automatic voice identification, parental filtering or home-scene automation in this extension.

## Limits that matter

The DJ uses **playlist names and explicit preferences**, not audio analysis or a claimed understanding of a song's energy. If a participant has no matching playlist, it reports that limitation and uses that person's general library. There is no promise of acoustic transitions or exact duration. The catalog is bounded to 30 playlists, 500 tracks per fetched playlist and 5,000 distinct tracks per profile; metadata refresh is about 15 minutes.

Feedback is local and does not write likes/history to Deezer. Full-track availability depends on the upstream API and the account; unavailable full streams fail explicitly instead of silently playing a preview. No guaranteed simultaneous-stream allowance or higher quality entitlement is added. The bridge uses normal MP3 quality. Music Assistant owns audio buffering, queues and multiroom synchronization. Transfer/seek accuracy depends on the player and stream. Multiroom audio and simultaneous independent-account streams require real equipment testing.

## Build and deployment

Tested with Python 3.14 / MA models 1.1.205 and the MA 2.10.9 base pinned in `deploy/Dockerfile.music-assistant`. A later MA release can change provider/setup APIs: test before upgrading. Build from this branch, not an upstream release archive.

```sh
mkdir -p build-home
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags='-s -w' -o build-home/opendeezer-home ./cmd/opendeezer-home
cp deploy/Dockerfile.bridge deploy/Dockerfile.music-assistant build-home/
cp -R music_assistant_provider build-home/
docker build -f build-home/Dockerfile.bridge -t local/opendeezer-home:0.3.0 build-home
docker build -f build-home/Dockerfile.music-assistant -t local/music-assistant-opendeezer:0.3.0 build-home
```

For ARM64, select `GOARCH=arm64` and a compatible base image. Only AMD64 has been deployed for this version.

1. Back up the existing MA data directory and Compose definition. Preserve the existing MA volume/path; do not create a second empty installation by accident.
2. Prepare `/srv/opendeezer/config`, `/srv/opendeezer/managed`, `/srv/opendeezer/admin` outside the repository. `config` and `managed`: UID/GID65532, directories0700, credential files0600. `admin`: traversable by the MA container and bridge; token file root:65532 mode0440, directory0750. Never expose these paths as web content.
3. Copy `deploy/config.example.json` into the private config directory. Set the first profile's ID/label and separate ARL/token file paths. Generate independent random profile and admin tokens (at least32 characters). The initial profile is required for bootstrap. Enter secrets privately, not in a command history, chat, issue or Compose environment variable.
4. Adapt `deploy/compose.example.yaml` to your existing paths and stack. Both containers use host networking on the same trusted Linux host, so the bridge stays on loopback8731. No bridge port is published. Both read the admin secret; only the bridge writes managed profiles.
5. Recreate **only these two services** with the new images. A full-stack redeploy is unnecessary. Keep old images and backups for rollback.
6. In Music Assistant, add **OpenDeezer Home** under music sources for the initial account (the enrollment flow recognizes the existing profile), then **OpenDeezer Famille** under plugins. The latter exposes `/opendeezer-family/` on the existing MA web port. Sign in with an existing MA administrator to manage accounts.
7. Add further members with the native form or optional browser companion. The original static config stays compatible; managed reconnect/revocation overrides take precedence.

The page itself is public static HTML, but all family data and commands require MA authentication and scopes. Account management requires `config.providers.write`. Pair submission alone is unauthenticated but requires an unguessable, short-lived, single-use ticket previously created by an administrator. Only an ARL is sent to the private bridge, not a Deezer password. Use HTTPS if traffic crosses an untrusted network. This is not a hosted multi-tenant service: MA users in the same household can access household mix/preferences data according to their MA role.

## Home Assistant

Use the native `rest_command` integration and scripts in `deploy/home_assistant/`; no new HA custom integration is needed.

1. Create a dedicated **non-administrator** MA user for voice control and a long-lived token for it. Store `Bearer <token>` as `opendeezer_voice_authorization` in HA `secrets.yaml`. Tokens expire according to MA policy (one year in tested MA); renew if authentication stops working.
2. Install `rest_commands.yaml` as `/config/opendeezer_rest_commands.yaml`; merge `rest_command: !include opendeezer_rest_commands.yaml` into your configuration without overwriting existing commands. Replace localhost8095 if MA is on another host.
3. Import each entry of `scripts.json` using HA's script configuration API/editor. Preserve existing scripts. Expose the six new scripts to Assist; a conversational agent with tool support is needed for natural free-form requests with fields.
4. Validate HA configuration. On first installation, HA must be restarted to load `rest_command`; later edits can use `rest_command.reload`.
5. Instruct the voice agent to call the mixed-playback script once with the named participants; no extra search is required. Use the profile-list script only for ambiguity. Visible playback actions need no spoken confirmation after success. **If the result contains `error` or `success:false`, explain the error instead.** Never infer a person's identity from voice.

Examples (replace names/rooms): “Mélange les goûts d'Alice et Bob dans le salon”, “Reprends la musique d'Alice”, “Bob aime ce morceau”, “Pourquoi ce morceau ?”. Selection is explicit, not biometric recognition. The UI offers adjustable weights; the voice script accepts a `poids` dictionary when the user explicitly requests weights.

## Tests and recovery

```sh
go test ./internal/homebridge/...
PYTHONPATH=music_assistant_provider:tests_home python -m unittest discover -s tests_home
```

Run Python tests in the exact installed MA virtual environment; HTTP fixtures are simulated and do not need any account. Tests cover isolation, enrollment, same-identity reconnect, persistent revocation, mixer fairness/bans/deduplication, preferences, ticket handling and scope registration. They are not a real microphone or multiroom benchmark.

Before replacing MA, snapshot the current queue and do not restore it over a newer user selection. On rollback restore the previous image tags; the 0.3 family state is separate from the music library. Back up private gateway config/managed files and MA data with restricted permissions. Never attach those backups to an issue. The family page's diagnostic export deliberately excludes account names, identifiers, ARLs, tokens and track history.

The source of these extensions is this repository. Report reproducible issues without private data and identify MA version, provider version and the failing operation. No automatic update or paid service activation is performed by this module.
