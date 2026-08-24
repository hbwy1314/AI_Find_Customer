# nginx site configs

- `ai-hunter.https.example.conf` — reference template for HTTPS deployments
  (with certbot-managed certs, HSTS headers, SSE-tuned proxy). Currently
  the live production deployment runs HTTP-only on port 80, so this
  file is NOT what nginx is loading — it's kept as a starting point for
  any future operator who wants to flip to HTTPS.

The live HTTP config lives on the server at
`/etc/nginx/sites-enabled/ai-hunter.conf` and is hand-maintained there
(it injects `X-API-Key` on `/api/` proxy_pass, which the example file
intentionally does not — see `docs/HTTPS_DEPLOY.md` for the prod setup).
