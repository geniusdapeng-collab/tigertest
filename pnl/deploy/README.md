# Tiger PnL deployment on a shared Linux host

The website is independent of the Python research worker and the WorkLoom governance application. Deploy only the `pnl/` subtree. Run `sync-research.mjs` from a complete Tiger checkout on the authorized Codex machine: its controlled CLI dependencies are outside `pnl/` and are deliberately absent from the small server release.

The prepared layout uses `/opt/tiger-pnl/releases/<full-source-sha>`, an atomic `/opt/tiger-pnl/current` symlink, an independent Node runtime copy at `/opt/tiger-pnl/runtime/node`, private records at `/var/lib/tiger-pnl`, backups at `/var/backups/tiger-pnl`, and root-only configuration at `/etc/tiger-pnl/runtime.env`. The unprivileged `tiger-pnl` system account runs one loopback process on port 4348 under an outer `flock`. Do not alter another application's service, runtime symlink, certificate or virtual host.

## Before installing

1. Confirm the exact host, available disk/memory, current Nginx configuration, occupied ports, existing applications' HTTP/HTTPS behavior, and the actual trusted Node 24 executable. Record this baseline for regression checks.
2. Resolve both `tigerpnl.com` and `www.tigerpnl.com` to this host. Check the authoritative nameserver and an external recursive resolver. Avoid publishing a stale IPv6 record.
3. Create `/etc/tiger-pnl` with mode 0700 and place the genuine service environment inside it as `runtime.env`, root-owned mode 0600. It contains `TIGER_PNL_WRITE_TOKEN`, `TIGER_PNL_DATA_DIR=/var/lib/tiger-pnl` and `PORT=4348`. Transfer the secret through a private upload or authenticated encrypted channel; never paste its plaintext into an audited terminal.
4. Package the reviewed, merged full commit's complete `pnl/` subtree, including the 4,104,128-byte registered whitepaper at `pnl/public/whitepaper/tiger-trading-20260730.pdf`, and record the exact archive SHA-256 before transfer. The installer rejects archive paths outside `pnl/`, links and other special entries. It does not download or execute a mutable branch.

A Git package can be produced with `git archive --format=tar.gz --output <private-archive-path> <full-merged-sha> pnl`. The SHA-256 must be calculated over those exact archive bytes. If a different transport repacks the source, independently verify every file against the merged commit and calculate the new archive hash before executing its installer.

## Loopback installation and rollback

Run the verified package's installer as root with four arguments:

```text
bash <verified-install.sh> <full-source-sha> <absolute-archive-path> <archive-sha256> <trusted-node-24-path>
```

The installer checks the source/archive identities, uses `/run/lock/tiger-pnl-deploy.lock` to exclude parallel install/activation, creates only Tiger's own paths and user, and performs a verified immutable-ledger backup before updating an existing service. It snapshots the current symlink, runtime and all three systemd units. Restart failure, unhealthy JSON or a later installation error restores those components and the previous enabled/running states, then checks the restored service. A first-install failure removes the new current/runtime/units and leaves the candidate and protected rollback snapshot for diagnosis. Existing release names and unmanaged real directories at `current` are refused rather than overwritten.

Successful installation writes `deployment.json` with the full source SHA, exact archive SHA-256 and loopback port. Check `systemctl status tiger-pnl.service` and `curl --fail http://127.0.0.1:4348/healthz`. A successful first install is still private: the public virtual host is activated only after genuine account and verified research records exist.

For an intentional rollback, verify the protected pre-update backup and selected previous release first. Reuse a reviewed install transaction or the saved rollback components under the same deployment lock; do not replace the live data directory or edit accepted record files. The website has no delete/overwrite API. A restore drill uses a separate private destination and verifies both chains before any separately authorized live replacement.

## TLS and public activation

Use `nginx-http.conf` as a dedicated exact-domain bootstrap virtual host. It serves only the ACME challenge directory and returns 503 for application requests. Validate Nginx before reloading it, and restore/remove only the new Tiger configuration if that check fails. Read the challenge from both domains on the public network before requesting the certificate.

Obtain a separate SAN certificate with Certbot's `certonly --webroot`, webroot `/var/lib/tiger-pnl-acme`, domains `tigerpnl.com` and `www.tigerpnl.com`, and certificate name `tiger-pnl-tigerpnl.com`. Reuse the host's already accepted ACME account when available. Preserve other applications' certificate lineages and the existing renewal timer. See the official [Certbot webroot guide](https://eff-certbot.readthedocs.io/en/stable/using.html#webroot).

Before activation, use the protected publisher from the full checkout to submit and independently read back one genuine account baseline and one completed, independently verified **model-required** result. A paper portfolio is never a genuine account source. Do not send a Bearer token over public HTTP.

Run `bash /opt/tiger-pnl/current/pnl/deploy/activate.sh` as root. It requires healthy JSON with at least one record in each chain and an existing certificate, snapshots the exact old Tiger virtual-host file, enabled symlink and renewal hook, checks the candidate, and reloads Nginx. Any configuration or reload failure restores those exact previous components and reloads the restored configuration. It changes only the Tiger virtual host and hook.

The HTTPS configuration uses exact server names, TLS 1.2/1.3, a 1 MiB body limit, bounded proxy timeouts and a loopback upstream. HTTP and HTTPS `www` requests redirect to `https://tigerpnl.com`, preserving the path and query. The renewal hook tests and reloads Nginx only for the Tiger certificate lineage.

To seed before public activation, install `nginx-bootstrap.conf` with the same saved-file/symlink rollback discipline and Nginx test/reload checks. It exposes only the authenticated `/api/v1/` API over the valid Tiger certificate and returns 503 for ordinary pages and public dashboard reads. An already authenticated private tunnel is another option. Never disable certificate validation. Complete the normal activation gate after the two verified first records exist.

## Operational verification

- Read the public homepage, static files, full registered PDF (compare exact SHA-256 and HEAD length), health and dashboard over certificate-validated HTTPS. Check the actual page title/body and source assets, not only a 200 status. Duplicate server-name warnings can leave another virtual host serving the domain.
- Verify HTTP and `www` path/query redirects, unauthenticated writes and private receipts returning 401, public projection redaction, and absence of credentials/account evidence in static assets.
- Restart Tiger and confirm both immutable chains retain the same counts and head hashes. Invoke `tiger-pnl-backup.service`, inspect success, and verify the resulting isolated backup. The timer runs daily at 03:35 UTC with a small randomized delay and catches up after downtime.
- Repeat the pre-install checks on every cohosted application. Do not infer their behavior from Tiger's health endpoint.
- Check desktop and mobile layouts in a real browser. Keep the real-account observation time distinct from the research data date. Missing new account evidence leaves the real account snapshot unchanged.

The application is intentionally long-only, single-currency USD, one server and one serialized writer. It does not authenticate brokers, execute trades, provide a multi-user admin UI, or prove the truthfulness of a token holder's attestations. Those boundaries are part of its operational contract.
