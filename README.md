# IndexHook

IndexHook receives recordings from a Pebble Index ring through the Index webhook, transcribes the audio, and runs rules on the text. Each user has their own token, settings, rules and history. All configuration is done on web pages.

A typical use is "note to self": say "note to self, renew the domain before Friday" into the ring, and an email with "Renew the domain before Friday." arrives a few seconds later.

## Contents

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Ring setup on Android](#ring-setup-on-android)
- [Speech to text](#speech-to-text)
- [Rules and actions](#rules-and-actions)
- [Accounts and login](#accounts-and-login)
- [Deployment](#deployment)
- [Security notes](#security-notes)
- [Development](#development)
- [Limits](#limits)

## How it works

1. The ring app POSTs `multipart/form-data` to `/hook` with the fields `audio` (M4A), `transcription` (the ring's own transcript, when it has one), `recordedAt` and `client`.
2. IndexHook finds the user from the token, writes the audio to disk and a row to SQLite, and answers `202` at once. The Index documentation does not describe timeouts or retries, so nothing slow happens before the answer.
3. A background worker transcribes the audio (see [Speech to text](#speech-to-text)).
4. The worker checks the user's rules against the transcript and runs the matching actions: email, ntfy, or a JSON POST to another URL.
5. Every step is stored. The Recordings page shows the audio, what each engine heard, the final text, and the result of each action.

State is in the database, so a restart continues with queued work. A second POST with the same `recordedAt` within ten minutes is treated as a duplicate.

## Quick start

Requirements: Python 3.11 or newer, and `ffmpeg` (optional, but audio clean-up and silence detection need it).

```sh
python3 -m venv venv
venv/bin/pip install .
venv/bin/indexhook --data-dir ./data serve --port 8787
```

Open `http://127.0.0.1:8787/`. The first account you create is the admin. Then:

1. **Settings**: paste one API key under **AI services** and press **Save and test**. The page reports whether the key works. Fill in the email (SMTP) details in the same step; the test also sends a test email.
2. **Overview**: create a webhook token. It is shown once.
3. **Test**: upload or record a clip and check the transcript.
4. Set up the ring (next section).

The ring app needs to reach the server over HTTPS from the phone, so put IndexHook behind a reverse proxy with a certificate. See [Deployment](#deployment).

Startup options (these are the only values not set on a web page):

| Option | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `--data-dir` | `INDEXHOOK_DATA_DIR` | `./data` | Database and audio files |
| `--host` | `INDEXHOOK_HOST` | `127.0.0.1` | Listen address |
| `--port` | `INDEXHOOK_PORT` | `8787` | Listen port |
| `--root-path` | `INDEXHOOK_ROOT_PATH` | empty | URL prefix when served under a sub-path, for example `/indexhook` |

## Ring setup on Android

These steps follow the Index help article "Index advanced features: MCP & webhook". The Overview page in IndexHook shows the same steps with your own URL filled in.

1. In IndexHook, open **Overview** and press **Create a token**. Copy the token. It cannot be shown again; you can replace it at any time.
2. On the phone, open the **Pebble** app and go to the **Index** tab. Open its settings.
3. In the **Webhook** section, set **Webhook URL** to the URL from the Overview page, for example `https://example.org/indexhook/hook`.
4. Under **Headers**, add one header:
   - Name: `X-Hook-Token`
   - Value: the token
   
   `Authorization` with the value `Bearer <token>` also works. Behind YunoHost, use `X-Hook-Token`: YunoHost's single sign-on layer can alter the `Authorization` header.
5. Set **Send** to **both** (recording and transcription). IndexHook transcribes the recording itself. The ring's transcript is kept as a fallback and shown for comparison. With "transcription only", IndexHook uses the ring's text as is.
6. Set **Trigger** to a button combination, or to all triggers. The Index agent still runs for every recording, so a dedicated combination keeps the two from both acting on the same note.
7. Make a short recording. It appears on the **Recordings** page within a few seconds.

Notes:

- The app allows one webhook.
- If the app cannot set headers for some reason, the token can go in the URL instead: `https://example.org/indexhook/hook/<token>`. Prefer the header, because URLs end up in proxy logs.
- If nothing arrives, test the server from a computer:

  ```sh
  curl -i https://example.org/indexhook/hook \
    -H "X-Hook-Token: ih_..." \
    -F "audio=@clip.m4a;type=audio/mp4" \
    -F "recordedAt=$(date +%s)000" -F "client=ring"
  ```

  `202` means the server accepted it. `401` means the token is wrong. An HTML login page means the proxy is not letting `/hook` through.

## Speech to text

Each recording goes through these steps. All of them are set per user under **Settings**.

1. **Measure.** ffmpeg reads the file and reports its length and peak level. A recording quieter than the silence threshold (default -50 dB) is marked "silent" and no engine is called. Speech engines tend to invent text for silent audio.
2. **Clean up** (on by default). The audio is converted to 16 kHz mono FLAC with a 70 Hz high-pass filter and loudness normalisation. The filter chain is mild. Strong noise suppression removes consonants and makes recognition worse.
3. **Transcribe.** The engine gets the audio along with your language, your vocabulary and your context sentence. If a cross-check engine is set, it runs at the same time on the same audio.
4. **Compare.** When two engines agree on less than 85% of the words, the recording is marked for review.
5. **Correct** (optional). A text model (MiniMax, Claude, OpenAI or your own server) gets every transcript of the recording (engine, cross-check, the ring's own) and your vocabulary. It is told to change a word only where another transcript or the vocabulary shows a mishearing. If its answer is less than 60% similar to the engine's transcript, the answer is dropped.
6. **Apply fixes.** Lines written as `heard => meant` in the vocabulary box are replaced in the final text.
7. **Fall back.** If every engine fails, the recording is retried (after 20 s, 90 s and 5 min for outages; not at all for a rejected API key). If there is still nothing, the ring's own transcript is used and the recording is marked for review. If there is no transcript of any kind, "always" rules still run and the email action attaches the audio.

### Services

Setting up a service takes one step: paste its API key under **Settings, AI services**. Base URLs and model names have working defaults and sit under "Advanced". **Save and test** sends each service that has a key a one-second test clip (or a one-line question for text models) and shows the answer or the error.

Speech engines:

| Service | Needs | Vocabulary is sent as | Notes |
| --- | --- | --- | --- |
| MiniMax | API key | not sent | Model `asr-1.0`. The API has no vocabulary or prompt parameter. Language is sent only when it is one MiniMax lists; otherwise the service detects it. Limits: 500 seconds and 50 MB per recording |
| Google Gemini Transcribe | API key | `custom_vocabulary` | Model `gemini-3.5-transcribe` (public preview). The audio is uploaded through Google's Files API, transcribed with one Interactions request that is not stored (`store: false`), and the uploaded copy is deleted afterwards. An optional "smart" style (under Advanced) drops filler words and resolves self-corrections |
| OpenAI | API key | a prompt | Model `gpt-4o-transcribe` |
| ElevenLabs Scribe | API key | `keyterms` | Model `scribe_v2`. ElevenLabs bills keyterms as an extra |
| Deepgram | API key | `keyterm` | Model `nova-3` |
| Groq | API key | a prompt | Hosted Whisper, model `whisper-large-v3` |
| Own server | URL (under Advanced) | a prompt | Any server that implements `/v1/audio/transcriptions`, such as speaches or whisper.cpp |

**Engine** is set to **Automatic** by default: the first service in the table that has a key does the transcription. Choose a specific one when you have several keys.

Text models for the correction pass:

| Service | Needs | Default model |
| --- | --- | --- |
| MiniMax | the same MiniMax key | `MiniMax-M3` |
| Claude | API key | `claude-haiku-5-5` |
| OpenAI | the same OpenAI key | `gpt-4o-mini` |
| Own server | URL and model (under Advanced) | none |

Claude is offered for the correction pass only. The Claude API accepts text and images, not audio, so it cannot transcribe a recording by itself. With a speech engine in front of it, Claude reads the transcripts and fixes misheard words.

With only a MiniMax key: MiniMax transcribes, and setting **Correction pass** to MiniMax makes its text model apply your vocabulary. This matters because MiniMax speech recognition takes no vocabulary hints.

An admin can enter keys once for everyone under **Admin, Server defaults**.

Where to get a key for each service, step by step, is in [deploy/yunohost.md, section 4](deploy/yunohost.md#4-keys-for-the-ai-services). Section 5 of the same file covers email, with Gmail as a worked example.

Published benchmarks disagree on which engine is the most accurate, and none of them used ring recordings. The Test page is there to answer the question with your own voice and your own microphone.

### Getting the best accuracy

- Use recordings from the ring for testing. Download one from a Recordings page, or use "Transcribe again" on a real recording after each settings change.
- Set the language. Short clips are sometimes detected as the wrong language.
- Fill in the vocabulary: names of people, places, products and projects you mention. This has the largest effect on proper nouns.
- Add a context sentence, for example "Short personal reminders about home, family and server administration."
- Set a cross-check engine from a different vendor. Two engines rarely make the same mistake, so a disagreement is a good sign that a recording needs a look.
- Turn on the correction pass if you see the engines disagree often, or if your engine takes no vocabulary (MiniMax).
- When the same mishearing keeps coming back, add a `heard => meant` line. This is also the fix for a trigger phrase that is misheard, for example `no to self => note to self`.
- Compare "Clean up audio" on and off on the Test page before changing it.

Every recording page lists what each source produced, so you can see which engine was right.

## Rules and actions

Rules are per user and are checked from top to bottom.

A rule matches when the transcript:

- **starts with a phrase** (any one of several, one per line),
- **contains a phrase**,
- **matches a regular expression**, or
- **always**.

Phrase matching ignores case and punctuation: "Note to self," and "note-to-self" both match `note to self`. A "starts with" rule can remove the trigger phrase from the text it passes on. By default a matching rule stops the checking; untick "stop" to let later rules also run.

Actions:

- **Email**: recipient (default: your address), subject template, optional audio attachment.
- **ntfy**: topic, title template, priority.
- **Webhook**: POSTs JSON (`text`, `full_text`, `first_words`, `date`, `time`, `rule`, `user`, `recording_id`, `recorded_at`, `needs_review`) to a URL with optional headers.

Templates can use `{text}`, `{first_words}`, `{date}`, `{time}`, `{rule}` and `{user}`.

A new account starts with two rules: "Note to self" (starts with "note to self", "notes to self", "note to myself", "note for self" or "remind me"; sends an email without the trigger phrase) and "Everything else" (always; sends an email). Delete the second one if you only want notes.

A failed action is retried after 1, 5, 30, 120 and 360 minutes. Permanent errors (wrong SMTP password, HTTP 4xx) are not retried. The recording page has a "Retry now" button. The Rules page has a "Try a phrase" box that shows which rules would match, without sending anything.

## Accounts and login

- IndexHook has its own accounts with passwords (scrypt hashes). The first account is the admin. Registration then closes unless the admin opens it under **Admin**. Admins can add, disable and delete users and reset passwords.
- Each user sees only their own recordings, rules and settings.
- **Settings** are per user. A blank field uses the server default, which admins set under **Admin, Server defaults**. A user can use an API key set there without being able to read it.
- **Reverse proxy login** (off by default): when the admin sets a header name, a request that carries that header and comes from a trusted proxy address is logged in as that user, with no password prompt. Unknown users can be created on first visit. This is how single sign-on from YunoHost, Authelia, Authentik and similar systems works. The webhook does not use this; it always uses the token.

Command line helpers for when the web page is not reachable:

```sh
indexhook --data-dir ./data create-user anna --email anna@example.org
indexhook --data-dir ./data set-password scott
indexhook --data-dir ./data set trusted_header Ynh-User
```

## Deployment

IndexHook is a single Python process with a SQLite database. Run one instance per data directory.

### systemd and nginx

`deploy/indexhook.service` is a systemd unit. `deploy/nginx.conf` is a location block for serving under `/indexhook/`. IndexHook accepts the prefix whether or not the proxy strips it.

### Docker

```sh
docker build -t indexhook .
docker run -d --name indexhook -p 127.0.0.1:8787:8787 -v indexhook-data:/data indexhook
```

When the proxy runs on the Docker host, requests reach the container from the Docker bridge address. Add that address to "Trusted proxy addresses" under Admin if you use reverse proxy login or want the session cookie marked Secure.

### YunoHost

See `deploy/yunohost.md`. In short: run IndexHook as a systemd service on `127.0.0.1:8787`, publish it with YunoHost's Redirect app in reverse proxy mode with public access, and optionally turn on reverse proxy login with the `Ynh-User` header. Nothing in IndexHook depends on YunoHost.

## Security notes

- Webhook tokens are stored as SHA-256 hashes. Passwords are stored as scrypt hashes. API keys and SMTP passwords are stored in the SQLite file in clear text, because the server has to use them. The data directory is created with mode 700 and the database with mode 600.
- Forms carry a CSRF token and cross-origin POSTs are refused. Login attempts are slowed down after five failures.
- Reverse proxy login trusts a header. Enable it only when IndexHook listens on an address that outsiders cannot reach directly (the default is 127.0.0.1). Any local process that can connect to the port could set the header.
- Users can make the server send requests to URLs they choose (engine base URL, ntfy server, webhook action). This is needed for local speech servers. On a server with users you do not trust, keep registration closed.
- Audio is kept for 30 days by default (Settings, Storage). Transcripts are kept until the recording is deleted.
- Recordings are sent to whichever speech engine you configure. For audio that must stay on your own hardware, point the OpenAI-compatible engine at a local Whisper server.

## Development

```sh
pip install -e ".[test]"
pytest
```

The tests start the app in-process, POST a ring-style request with a real M4A file made by ffmpeg, replace the speech engines and other HTTP services with fakes, and deliver email to a local SMTP sink.

Layout:

| File | Purpose |
| --- | --- |
| `indexhook/app.py` | Routes: webhook, login, pages |
| `indexhook/stt.py` | Engines, vocabulary, comparison, correction pass |
| `indexhook/audio.py` | ffmpeg measuring and clean-up |
| `indexhook/rules.py` | Rule matching |
| `indexhook/actions.py` | Email, ntfy, webhook |
| `indexhook/worker.py` | Background queue, retries, audio retention |
| `indexhook/config.py` | Every setting, its default and help text |
| `indexhook/auth.py` | Passwords, sessions, tokens, proxy login |

To add a speech engine, write one function in `stt.py`, add it to `ENGINE_FUNCS`, and add its settings to `config.py`.

## Limits

- The webhook request does not say which button combination was used, so rules can only match on the text.
- The ring's behaviour on a slow or failed webhook response is not documented. IndexHook answers fast and ignores duplicates by `recordedAt`.
- The service integrations follow each vendor's published API. They are tested against fakes that check the request format. Each live endpoint was also called with an invalid key and answered "wrong key", which confirms the address and the way the key is sent. No call with a valid key has been made yet, so the first real check is **Save and test** with your own key. The Google endpoint could not be reached from the build environment, so it has only been tested against a fake.
- An action that was being sent at the moment of a crash is sent again after restart, so a note can arrive twice in that case.
