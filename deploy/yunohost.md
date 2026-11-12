# IndexHook on YunoHost

Sections 4 (keys for the AI services) and 5 (email, with Gmail as an example) apply to any install, not only YunoHost.

This is a manual install. It has not been tested on a YunoHost server yet, and it is not a YunoHost package. The steps use only standard parts: a systemd service and the Redirect app.

## 1. Install the service

```sh
sudo apt install ffmpeg python3-venv
sudo useradd --system --home /var/lib/indexhook --create-home indexhook
sudo mkdir -p /opt/indexhook && sudo chown indexhook: /opt/indexhook
sudo -u indexhook git clone <your repository URL> /opt/indexhook/src   # or copy the files there
sudo -u indexhook python3 -m venv /opt/indexhook/venv
sudo -u indexhook /opt/indexhook/venv/bin/pip install /opt/indexhook/src
sudo cp /opt/indexhook/src/deploy/indexhook.service /etc/systemd/system/
```

Edit `/etc/systemd/system/indexhook.service` and set `INDEXHOOK_ROOT_PATH` to the path you will use in step 2 (for example `/indexhook`), or leave it empty if IndexHook gets its own domain. Then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now indexhook
curl http://127.0.0.1:8787/healthz
```

Debian 12 ships Python 3.11, which is new enough.

## 2. Publish it

Install the **Redirect** app (`redirect_ynh`) from the YunoHost catalogue:

- Domain and path: for example `example.org` and `/indexhook`
- Redirect type: reverse proxy (invisible, nginx `proxy_pass`)
- Target: `http://127.0.0.1:8787`
- Access: **visitors** (public)

Public access is required. The ring cannot log in to the YunoHost portal, so a private app would answer the webhook with a redirect to the login page. IndexHook has its own login for the web pages, and the webhook needs the per-user token.

IndexHook accepts requests with or without the `/indexhook` prefix, so it does not matter whether the proxy strips it.

## 3. First login

Open `https://example.org/indexhook/` and create the first account. It becomes the admin.

Under **Admin, Server**, set **Public address** to `https://example.org/indexhook` if the webhook URL shown on the Overview page is not right.

## 4. Keys for the AI services

You need a key for at least one speech service. Each person can paste their own keys under **Settings, AI services**. An admin can instead paste keys once under **Admin, Server defaults**, and every user then uses them without being able to read them.

After pasting a key, press **Save and test**. The page reports for each service whether the key works.

Every service below shows a new key only once, at the moment it is created. Copy it straight into IndexHook. Menu names on these sites change from time to time; the addresses are the stable part.

### MiniMax (speech, and optionally the correction pass)

MiniMax has two kinds of key. Either one works in IndexHook, in the same **MiniMax API key** field, with no other setting to change. They bill differently and cannot stand in for each other on MiniMax's side, so pick the one that matches how you pay.

**Option A: Subscription Key (Token Plan, also shown as M Plan)**

Use this if you already pay for a MiniMax subscription. Speech recognition and the text models draw on the plan's shared monthly quota.

1. Sign in at <https://platform.minimax.io>.
2. Open **Billing, Token Plan**. Direct link: <https://platform.minimax.io/user-center/payment/token-plan>
3. Copy the **Subscription Key** shown there.
4. Paste it into **MiniMax API key**.

The key exists even before you subscribe, but it only works once the account has a plan seat or purchased Credits. The quota is shared with everything else you use the subscription for, such as coding tools, and it has usage windows. When the quota runs out, transcription fails until the window resets, and IndexHook falls back to the ring's own transcript and marks the recording for review. Purchased Credits, if you have any, are used after the quota.

**Option B: pay-as-you-go API key**

Use this if you have no subscription, or if you want the ring to keep working no matter how much of the subscription quota other tools have used. It is billed from the account balance by the hour of audio.

1. Sign in at <https://platform.minimax.io>.
2. Open **API Keys** and choose **Create new secret key**. Direct link: <https://platform.minimax.io/user-center/basic-information/interface-key>
3. Add credit under **Billing, Balance** if the balance is zero.
4. Paste the key into **MiniMax API key**.

**Checking it**

Press **Save and test**. If the answer is "the key was not accepted", the usual causes are a Subscription Key on an account with no active plan or Credits, or a mistyped key. If the answer is "the account has no credit", top up the balance (Option B) or check the plan's usage bar (Option A).

If your account balance is being charged although you have a subscription, the pay-as-you-go key is the one in use. Replace it with the Subscription Key.

Accounts on the China platform (platform.minimaxi.com) also set **Advanced, MiniMax base URL** to `https://api.minimaxi.com/v1`.

### Google Gemini (speech)

1. Sign in at <https://aistudio.google.com/apikey> with a Google account.
2. Choose **Create API key**. Pick an existing Google Cloud project or let AI Studio create one.
3. Paste the key into **Google Gemini API key**.

A key works without a billing account inside the free tier's rate limits. Two things to know about the free tier: in some regions, including much of Europe, the API reportedly does not work until billing is enabled on the project, and Google may use free-tier data to improve its products. Enable billing on the project (AI Studio links to it) if either matters to you.

### OpenAI (speech, and optionally the correction pass)

1. Sign in at <https://platform.openai.com>.
2. Add credit under **Settings, Billing**. The API is billed separately from a ChatGPT subscription.
3. Open <https://platform.openai.com/api-keys> and choose **Create new secret key**.
4. Paste the key into **OpenAI API key**.

### Claude (correction pass only)

1. Sign in to the Claude Console at <https://platform.claude.com>, or create an account.
2. Make sure the organization has credit. Buy credits on the Billing page (<https://platform.claude.com/settings/billing>). Claude Max, Ultra and Team plans include monthly API credits: claim them on claude.ai under **Settings, Billing, API credits** by linking the Console organization. A Claude subscription does not otherwise pay for API use.
3. Open the API keys page (<https://platform.claude.com/settings/keys>) and choose **Create key**.
   - Expiration: choose **Never**, or note the date. An expired key makes the correction pass fail (the transcript from the speech engine is still used).
   - Workspace: select one, for example the default workspace. A key that is not tied to a workspace needs the workspace ID sent with every request; in that case paste the ID (it starts with `wrkspc_`) into **Advanced, Claude workspace ID**.
4. Paste the key (it starts with `sk-ant-`) into **Claude API key**, and set **Correction pass** to Claude.

### ElevenLabs (speech)

1. Sign in at <https://elevenlabs.io> and open <https://elevenlabs.io/app/developers/api-keys>.
2. Choose **Create Key** and give it a name.
3. If you restrict the key's permissions, give **Speech to Text** access. A restricted key with no permissions does not work.
4. Paste the key into **ElevenLabs API key**.

### Deepgram (speech)

1. Sign in at <https://console.deepgram.com>.
2. In your project, open **Settings, API Keys** and choose **Create a New API Key**. The Member role is enough.
3. Paste the key into **Deepgram API key**.

### Groq (speech, hosted Whisper)

1. Sign in at <https://console.groq.com> and open <https://console.groq.com/keys>.
2. Choose **Create API Key** and give it a name.
3. Paste the key into **Groq API key**.

### Which key is used

With **Engine** set to Automatic, the first service in this order that has a key transcribes: MiniMax, Google, OpenAI, ElevenLabs, Deepgram, Groq, own server. To use a specific one, choose it under **Speech to text, Engine**. A second service can be set as **Cross-check engine**.

## 5. Email

Email is how the starter rules deliver notes. Set it per user under **Settings, Email**, or once for everyone under **Admin, Server defaults, Email**. **Save and test** sends a test message.

### Option A: YunoHost's own mail server

YunoHost runs a mail server on the same machine:

- SMTP server: `localhost`
- SMTP port: `25`
- Encryption: None
- SMTP user and password: leave blank
- From address: an address on one of your YunoHost domains

### Option B: Gmail

Gmail accepts SMTP logins only with an app password (or OAuth, which IndexHook does not use). Your normal Google password does not work here.

1. Turn on 2-Step Verification for the Google account: <https://myaccount.google.com/security>. App passwords are not offered without it.
2. Open <https://myaccount.google.com/apppasswords>, enter a name such as "IndexHook", and create the password. Google shows 16 letters in four groups.
3. In IndexHook, set:

   | Field | Value |
   | --- | --- |
   | Send notes to | the address the notes should arrive at (can be the same Gmail address) |
   | SMTP server | `smtp.gmail.com` |
   | SMTP port | `587` |
   | Encryption | STARTTLS |
   | SMTP user | your full Gmail address |
   | SMTP password | the 16 letters, typed without the spaces |
   | From address | leave blank (the SMTP user is used) |

   Port `465` with Encryption set to SSL/TLS works as well.
4. Press **Save and test** and check that the test message arrives.

Notes on Gmail:

- The app password page is missing when 2-Step Verification is off, when the account uses only security keys for it, when Advanced Protection is on, or when a Google Workspace admin has disabled app passwords.
- Gmail sends as the account that logged in. A different From address is replaced unless it is set up as a "Send mail as" alias in Gmail.
- "SMTP login failed" in the test result means Gmail refused the user name or password. The usual causes are the normal password in place of an app password, or a revoked app password. Changing the Google account password revokes all app passwords.
- A personal Gmail account may send about 500 messages a day, which is far more than notes need.
- If the account holds anything sensitive, a separate Gmail account used only for sending keeps the app password's reach small. An app password gives full mail access to its account.

## 6. Single sign-on (optional)

YunoHost's SSO layer (SSOwat) adds the headers `Ynh-User` and `Ynh-User-Email` to requests from people who are logged in to the portal. To let those people into IndexHook without a second password, set under **Admin, Reverse proxy login**:

- User name header: `Ynh-User`
- Email header: `Ynh-User-Email`
- Trusted proxy addresses: `127.0.0.1, ::1`
- Create an account on first visit: on

If the web page is not reachable, the same from a shell:

```sh
sudo -u indexhook /opt/indexhook/venv/bin/indexhook --data-dir /var/lib/indexhook set trusted_header Ynh-User
sudo -u indexhook /opt/indexhook/venv/bin/indexhook --data-dir /var/lib/indexhook set trusted_header_email Ynh-User-Email
```

Things to check on your server before relying on this:

- That the Redirect app forwards the `Ynh-User` header to the target. If IndexHook still shows its own login page while you are logged in to the portal, add `proxy_set_header Ynh-User $http_ynh_user;` and `proxy_set_header Ynh-User-Email $http_ynh_user_email;` to the app's nginx location and reload nginx.
- That a visitor who is not logged in cannot inject the header. SSOwat is documented to remove authentication headers for unauthenticated visitors of public apps. Test it: `curl -H "Ynh-User: yourname" https://example.org/indexhook/` must return the redirect to `/indexhook/login`, not the Overview page.

A YunoHost account that matches an existing IndexHook user name logs in as that user. Name the first admin account after your YunoHost user for that reason.

## 7. The ring

Use the `X-Hook-Token` header in the Pebble app (see the README). SSOwat can alter `Authorization` headers on their way to an app, and `X-Hook-Token` is not affected.

## Backups

Everything is in `/var/lib/indexhook`: `indexhook.sqlite3` (plus `-wal` and `-shm` files while running) and the `audio` folder. Stop the service or use `sqlite3 indexhook.sqlite3 ".backup copy.sqlite3"` for a consistent copy. The Redirect app's own backup does not include this folder.
