# IdentityOS local phone adapter

One SIP extension reaches the runtime switchboard. Identity aliases route to
existing canonical identity IDs; no identity owns a telephone number. This is a
local SIP prototype, with provider-independent SMS command ingress. Carrier SMS
and PSTN numbers are not provisioned by this implementation.

## Architecture and integration

```
Linphone -- LAN SIP/RTP --> Asterisk
  -> loopback FastAGI caller registration
  -> single-use call UUID -> loopback AudioSocket PCM/DTMF
  -> local VAD -> local Whisper -> VoiceCall -> IdentityRouter
  -> InteractionRequest -> IdentityRuntime.process -> local Ollama
  -> local Piper -> paced AudioSocket playback -> phone
```

- `core/channels/router.py`: caller/user mappings, aliases, preferred identity,
  active SMS identity, independent session IDs, scrypt PIN hashes and lockouts.
- `core/channels/context.py`: transient request context and additive channel
  capability policy. The model cannot raise authentication by claiming success.
- `runtime/phone/switchboard.py`: voice/DTMF/SMS commands using that shared router.
- `runtime/phone/audio.py`: SpeechToText, TextToSpeech, VoiceActivityDetector and
  TelephonyTransport protocols, whisper.cpp/Faster Whisper, Piper and energy VAD.
- `runtime/phone/asterisk.py`: bounded FastAGI registration, one-use media tickets,
  concurrent input and playback, barge-in and connection cleanup.
- `cli/phone.py`: local administration and service entry point.

`InteractionRequest.channel_context` is optional and appended after existing
fields. Existing terminal/web/API callers retain their behavior. Metadata reaches
context composition, not the user text or semantic memory input. For channel
requests, assistant output is excluded from semantic and identity-mutation
extraction so a description of the current interface cannot become permanent
self-knowledge. User facts and normal episodic conversation still use the existing
runtime. Call identifiers and PIN digits are excluded from model context.

Routing/authentication state lives in `routing.sqlite3` next to the phone config,
separate from identity persistence. Voice sessions are independent per identity
and call; switching away and back reuses the session. Hangup ends those sessions.
SMS sessions and caller preferences survive process restart. A restarted voice
call requires a new SIP connection and fresh PIN authentication.

The phone service owns its runtime instance and serializes runtime turns because
its mutable stores are not generally thread-safe. Audio reception and cancellation
remain asynchronous while the runtime runs in a worker thread. In-flight runtime
work is allowed to finish on hangup; it cannot safely be undone by cancelling TTS.
Calls are bounded to one hour, utterances to 30 seconds, pending turns to four,
and active calls to four. Overload or provider errors end the affected call.

## Free local setup

Download dependencies/models once; calls then need no external network. Use an
Asterisk release whose AudioSocket application forwards DTMF (22.6 or newer is
recommended; Ubuntu's older 20.6 package does not forward DTMF). Required modules
include `res_agi`, `res_speech`, `res_audiosocket`, `app_audiosocket`, PJSIP,
RTP, `func_callerid`, and the ulaw/linear codecs plus their dependencies. Check installed modules:

```sh
asterisk -rx 'module show like audiosocket'
asterisk -rx 'core show application AudioSocket'
python3 -m pip install -e '.[phone,dev]'
python3 -m piper.download_voices --data-dir "$HOME/idos-models" en_US-lessac-medium
python3 -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-tiny.en', local_dir='local-whisper-tiny.en')"
ollama list
```

Choose an already installed Ollama model. A larger Whisper model may improve
recognition of names. `serve` loads Faster Whisper with `local_files_only=True`;
it does not silently acquire models. Alternatively set STT to:

```json
{"backend":"whisper.cpp","executable":"/absolute/path/whisper-cli","model":"/absolute/path/ggml-base.en.bin"}
```

For a separate local OpenAI-compatible server such as llama.cpp, set
`model_base_url` to its loopback HTTP API (for example
`http://127.0.0.1:11435/v1`) and `model` to its configured alias. The default is
local Ollama on port 11434. Remote endpoints and URL credentials are rejected.
`model_timeout` overrides the default 120-second model timeout for slower local
hardware; increasing it does not improve latency.
Models without reliable native tool calling can select `tool_mode: "legacy"`.
This uses the existing Ollama adapter's bounded text-tool loop, including the same
runtime capability validation and evidence handling; it does not grant extra
permissions. The default remains `native`. Use the model's embedded chat template
when serving it with llama.cpp; with `--jinja`, a literal template name may be
treated as template text rather than a built-in template selection.

Create or select identities using the existing CLI. Then copy
`examples/phone/config.json` to `.identity_phone/config.json`, fill in **canonical
identity IDs**, local model paths, and executable path. Aliases must be lowercase
letters/digits/underscore/hyphen. Their configuration order defines DTMF choices
(1–9 supported). Do not reorder a live directory unexpectedly.

```sh
identity --store .identity_store list
identity phone bind --caller 1001 --user arsene --identity aster
identity phone pin set --user arsene
identity phone default --user arsene --identity aster
identity phone routes
identity phone callers
identity phone status
identity phone provider list
identity phone provider configure --reference /absolute/path/to/pjsip.conf
identity --store .identity_store phone serve
```

PIN setup prompts securely, with confirmation. No plaintext PIN enters the
configuration, command line or identity state. `status` reports configuration,
not an assertion that a call works. Provider configuration records only a file
reference; it does not edit/reload Asterisk or read credential contents.

## Asterisk and Linphone

Merge the examples under `examples/phone/asterisk/` into an existing local PBX;
do not overwrite an unrelated dialplan. Replace `LAN_IP` with the laptop's private
LAN address and choose a strong local SIP password. Keep both FastAGI (4573) and
AudioSocket (9092) bound to loopback. Restrict SIP and RTP to your trusted LAN;
no port forwarding, public tunnels, SIP trunk or external account is required.

In Linphone, add a SIP account using username `1001`, the configured SIP password,
and the laptop's LAN address as domain/proxy. Disable external proxy/STUN for this
LAN test and use PCMU/ulaw. Phone and laptop must be mutually reachable on the
same LAN. Call `sip:700@LAN_IP`. The sample dialplan fixes the caller mapping to
the authenticated endpoint, preventing a forged SIP From header from selecting
another user's routing record. Additional users need their own authenticated
endpoint/context and caller binding, **not** separate identity phone numbers.

Speak “Talk to Aster”, “Get me Gabriel”, “Switch to Gabriel”, “Switch me to Aster”,
“Who can I talk to?”, or “Switch identity”. A bare configured alias also works.
Press 1/2 for identities, 0 for directory, or `*`, PIN digits, `#` for authentication.
Normal speech goes directly to the selected/default identity. Selection during a
call does not silently change the caller's permanent default.

## Barge-in

Incoming speech immediately invalidates paced playback and cancels TTS synthesis.
Only 20 ms of audio is sent per tick, limiting already-transmitted playback;
AudioSocket has no remote buffer-clear acknowledgement. New speech suppresses
obsolete responses while preserving call/session state. Simple energy VAD is
sensitive to speaker echo and noise; use a headset/earpiece, then tune
`vad_threshold`. This prototype does not implement acoustic echo cancellation.

## SMS routing and provider adapters

`Switchboard.sms(caller, text)` is the reusable ingress API for an authenticated
provider adapter. Exercise it locally without a carrier:

```sh
identity phone sms --caller 1001 '/identities'
identity phone sms --caller 1001 '/id gabriel'
identity phone sms --caller 1001 '@aster Hello'
identity phone sms --caller 1001 'Hello again'
identity phone sms --caller 1001 '/status'
identity phone sms --caller 1001 '/new'
```

`/id NAME` changes the persistent active SMS identity; `@NAME MESSAGE` routes one
message without changing that selection or the caller's default. `/new` rotates
only the active identity's SMS session. `/id`, `/identities`, `/status`, `/help`
are available without model calls. Unknown commands fail explicitly. This CLI
is an ingress harness, **not evidence that a cellular SMS was sent**. The current
voice context truthfully sets `sms_available=false`.

A future Twilio/Telnyx/SignalWire adapter must verify provider signatures, resolve
caller identity, enforce replay/idempotency protections and call the same
switchboard. Generic SIP trunks terminate at Asterisk. PSTN deployment also
requires carrier provisioning, TLS/SRTP and network hardening, which this LAN
prototype does not perform. Do not expose the loopback gateway publicly.

## Authentication and capability policy

Unknown callers are rejected until locally enrolled. Recognized callers may use
explicitly reviewed read/calculation skills. Caller ID does not permit sensitive
actions. PINs use scrypt with independent random salts, persistent failure counts
and a five-minute lockout after five failures. PIN verification lasts five minutes
within the current call and is cleared on disconnect. PIN entry expires after 30
seconds; digits are never submitted to STT, models, memory or normal logs.

Channel capability policy is a closed exact-skill allowlist. Email read/send
require PIN; unknown tools, financial actions, shell/system writes, capability
creation and meta-execution are denied even with PIN. Existing capability grants
are also required. Add a reviewed skill to `authorize_skill` to extend the policy;
never classify arbitrary generated tools as safe from their name alone.

Phone requests disable automatic reflex dispatch and Prometheus acquisition.
They still enter the normal runtime; this restriction prevents indirect background
execution from escaping transient caller authority. Supporting those actions
requires propagating authenticated scope through durable tasks first. Already
existing background work remains governed by the runtime's existing policies.

Loopback peers and the machine account running Asterisk are trusted. Protect the
SQLite/config files and disable PBX SIP/DTMF packet logging when entering PINs.
LAN SIP audio is unencrypted in the sample. PINs control tools, not access to all
historical conversational content: enroll only trusted users and use authenticated
SIP endpoints. Public or multi-tenant deployment needs a separate access review.

## Verification

Run `python3 -m pytest tests/test_phone.py -q` and the repository suite. Tests cover
routing, caller isolation, SMS semantics, DTMF, per-identity voice sessions,
context, memory separation, PIN/lockout, actual capability enforcement,
AudioSocket failure handling, paced playback cancellation and fresh-process state.

Manual acceptance on Linphone:

1. Call 700; hear the directory and talk with the default identity.
2. Select the other identity by voice and DTMF; switch back and verify continuity.
3. Ask what interface is in use; verify a concise voice-aware response.
4. Interrupt speech; verify playback stops and the next response addresses you.
5. Try an incorrect PIN, then the configured PIN; verify explicit outcomes.
6. Hang up/reconnect; verify preference survives and PIN authority does not.
7. Stop/restart the gateway; verify another successful spoken runtime interaction.
8. Stop the STT/TTS backend or send an invalid ticket; verify explicit failure,
   not invented audio/results. Verify terminal chat still works independently.

Record real observations, versions and limitations. Synthetic PCM tests and a
passing suite do not by themselves establish that a physical phone call passed.

For operator-run software endpoint testing, `scripts/phone_sip_probe.py` (Python
3.11/3.12) sends actual SIP, PCMU RTP and RFC4733 keypad events to a **loopback-only**
test PBX on port 15060, extension 700, caller binding 1001. It uses local Piper to
speak a question and local Whisper to transcribe returned audio, saving a WAV for
inspection. Supply the same local model configuration with `--config`, and add
`--dtmf` to select the second directory entry before the question. This separate
test endpoint may use loopback IP identification; never use that shortcut on a
LAN-facing listener. This is not a physical microphone/earpiece acceptance test.

Whisper receives the configured identity names as vocabulary hints; these do not
guarantee exact name spelling. For stronger end-to-end verification, pass
`--store /path/to/the/live/json/store` to the probe. It then requires a **new**
persisted response from the selected canonical identity for the spoken question,
and at least 70% normalized text similarity between that response and the returned
audio transcription. This tolerates phonetic name spelling without accepting an
old memory, a greeting, or a response belonging to another identity. Without
`--store`, the probe retains its exact-name transcription assertion.

Protocol references: [Asterisk AudioSocket framing](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/),
[AudioSocket dialplan application](https://docs.asterisk.org/Latest_API/API_Documentation/Dialplan_Applications/AudioSocket/),
[local Faster Whisper](https://github.com/SYSTRAN/faster-whisper), and
[Piper](https://github.com/OHF-Voice/piper1-gpl). Models and optional speech tools
have their own licenses; consult their model cards before redistribution.

### Implementation verification observations

The final repository suite passed with 1,054 tests passed and 33 skipped;
Ruff passed for the new phone modules, probe and phone tests.

An operator-run loopback SIP probe completed the full audio path using Asterisk
22.11.0, Faster Whisper tiny.en, Piper en_US-lessac-medium and Ollama llama3.2:1b.
The greeting selected Aster, an RFC4733 keypad event selected Gabriel, and the
spoken question "Hello, what is your name?" produced returned RTP speech identifying
Gabriel. The corresponding user/assistant exchange was independently loaded from
the existing JSON identity persistence backend. STT took about 1.1 seconds and
the runtime/model turn about 29 seconds on the test machine. This establishes a
working local path, but that latency is not yet a smooth conversation experience.

The temporary PBX installation and raw logs were lost between work sessions;
these are recorded observations, not retained audio artifacts. A subsequent
operator-run CLI check used separate processes to enroll a caller, change its
default from Aster to Gabriel, and reload that preference successfully. Regression
tests additionally exercise fresh-process SMS session/PIN reuse, failure handling
and barge-in. The live call did not verify physical-phone microphone/earpiece
behavior, live barge-in, or a gateway restart followed by another spoken call;
those remain items in the Linphone checklist above. No carrier number, carrier
SMS delivery or public listener was configured.
