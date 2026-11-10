# IndexHook on YunoHost

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

## 4. Email

YunoHost runs its own mail server. Under **Admin, Server defaults, Email** set:

- SMTP server: `localhost`
- SMTP port: `25`
- Encryption: None
- From address: an address on one of your YunoHost domains

Every user then gets working email without entering anything.

## 5. Single sign-on (optional)

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

## 6. The ring

Use the `X-Hook-Token` header in the Pebble app (see the README). SSOwat can alter `Authorization` headers on their way to an app, and `X-Hook-Token` is not affected.

## Backups

Everything is in `/var/lib/indexhook`: `indexhook.sqlite3` (plus `-wal` and `-shm` files while running) and the `audio` folder. Stop the service or use `sqlite3 indexhook.sqlite3 ".backup copy.sqlite3"` for a consistent copy. The Redirect app's own backup does not include this folder.
