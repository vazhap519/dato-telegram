# Telegram Mini App — Nginx route

The live Nginx configuration is stored outside the Git working tree in
`/etc/nginx/sites-enabled/dato-telegram`. The canonical, idempotent migration
to allow Telegram-native authorization for the Mini App is versioned at
`scripts/configure_support_miniapp_nginx.py`.

## Apply after app deployment

```bash
/usr/local/bin/update-dato-telegram &&
sudo /usr/bin/python3 /opt/dato-telegram/scripts/configure_support_miniapp_nginx.py
```

The script:
- Checks that the expected HTTPS site's existing Basic Auth still exists.
- Inserts exact `/tg-support` and prefix `/tg-support/` locations, without
  relaxing authentication for any other path.
- Backs up the original configuration outside `sites-enabled`.
- Performs `nginx -t`, reverts if invalid, then reloads Nginx.
- Is idempotent and intentionally refuses unexpected/partial configs.

These two endpoints do NOT require Basic Auth, but the API still requires the
Telegram Web App `initData` HMAC signature and an enabled, linked staff user.
An anonymous request must receive `401` JSON, never private ticket data.

## Verify

```bash
curl -sSI https://bot.arutyun.me/tg-support/api/me
curl -sSI https://bot.arutyun.me/login
```

Expected:
- `/tg-support/api/me`: **401 application/json**, without `WWW-Authenticate: Basic`.
- `/login`: **401** with `WWW-Authenticate: Basic`.

Then open the support Mini App through the Telegram menu.

Never commit the server's private Nginx basic-auth credential file or passwords
to this repository.
