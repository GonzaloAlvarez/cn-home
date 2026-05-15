# cn-home

LAN edge for `kaiser.lan`. Owns three things:

1. **Traefik** on `kaiser:80/443` — terminates TLS via step-ca ACME (HTTP-01) and routes every `*.kaiser.lan` subdomain to the appropriate `127.0.0.1:<port>` binding.
2. **Dashy** at `https://home.kaiser.lan` — homelab dashboard, fully YAML-configurable in [`dashy/conf.yml`](dashy/conf.yml).
3. **pfSense Unbound Domain Override** that makes `*.kaiser.lan` resolve to the kaiser host via [`domain.py`](domain.py) (a port of `amun-kubernetes/domain.py`).

Sibling repos route traffic through this Traefik by binding their services on `127.0.0.1:<port>` on the same host. See [`traefik-lan/dynamic.yml.tmpl`](traefik-lan/dynamic.yml.tmpl) for the routing contract.

## Architecture

```
LAN
├── pfSense 10.1.0.1                Unbound override:  kaiser.lan → 10.1.1.140
├── pki.lan 10.0.0.192               step-ca (HTTP-01 ACME, root CA at /cert/ca.crt)
└── kaiser.lan 10.1.1.140
    └── cn-home  (this repo)
        ├── traefik-lan (network_mode: host, :80/:443, step-ca ACME)
        └── dashy (127.0.0.1:8085 → routed as home.kaiser.lan)
```

## Subdomains in v1

| Hostname | Routes to | Owned by |
|---|---|---|
| `home.kaiser.lan` | `http://127.0.0.1:8085` | cn-home (Dashy) |
| `grafana.kaiser.lan` | `http://127.0.0.1:3000` | [cn-observability](https://github.com/GonzaloAlvarez/cn-observability) |
| `prometheus.kaiser.lan` | `http://127.0.0.1:9090` | cn-observability |
| `alertmanager.kaiser.lan` | `http://127.0.0.1:9093` | cn-observability |
| `loki.kaiser.lan` | `http://127.0.0.1:3100` | cn-observability |
| `portainer.kaiser.lan` | `http://127.0.0.1:9000` | cn-observability |

## Deploy

### One-time

```sh
./setup.sh          # writes .env (prompts), fetches step-ca root CA, renders templates
```

You'll be asked for `ADMIN_EMAIL` and confirm the LAN topology defaults. The script grabs `http://${PKI_IP}/cert/ca.crt` (plain HTTP, LAN-only) and saves it to `certs/root_ca.crt` — Traefik needs this so its Lego client trusts step-ca for ACME chain validation.

### Every deploy

```sh
./deploy                       # cn-home only
./deploy --with-observability  # also deploys the sibling cn-observability/
./deploy --skip-dns            # skip the pfSense step (e.g., re-running)
./deploy --skip-verify         # skip the post-deploy curl checks
```

What `./deploy` does, in order:

1. `python3 ./domain.py` — programs pfSense Unbound Domain Override for `${LAN_DOMAIN} → ${KAISER_IP}` (single override covers the whole zone). Caches creds at `.pf-creds` (chmod 600); prompts once.
2. `rsync` this repo to `${KAISER_SSH}:${KAISER_REMOTE_DIR}` (skipping `.env`, `.pf-creds`, `.git`, the throwaway venv).
3. `scp` the `.env` separately so it's not in any rsync log.
4. `ssh kaiser docker compose -p cn-home up -d`.
5. Verification: DNS, TLS handshake against each subdomain, SSL verify result.

If `--with-observability`, repeats 2–4 for `../cn-observability/` **before** cn-home (so the backends are listening when Traefik first tries to route).

## How to add a new service

The pattern, repeating the same shape for every backend:

1. New service binds its HTTP port to `127.0.0.1:<port>` on the kaiser host (in its own `docker-compose.yml`).
2. Add a router + service block to [`traefik-lan/dynamic.yml.tmpl`](traefik-lan/dynamic.yml.tmpl) under the `LLM-AGENT EDIT TARGET` comment, pointing at that port.
3. Add a Dashy entry to [`dashy/conf.yml`](dashy/conf.yml) under the appropriate section.
4. `./deploy --skip-dns` from the workstation. Traefik file provider has `watch=true`, so even without a restart it picks up the new route within a second; Dashy needs a container restart to reload `conf.yml`.

step-ca will request a new 24h cert on first request to the new subdomain via HTTP-01. No DNS work needed because the Unbound override already catches every subdomain in the zone.

## Host prerequisites (one-time on kaiser)

The new stack needs three privileged ports open. UFW on Debian denies by default; before the first `./deploy` either run:

```sh
ssh gonzalo@kaiser.lan
sudo ufw allow 53/udp comment "cn-home CoreDNS"
sudo ufw allow 53/tcp comment "cn-home CoreDNS"
sudo ufw allow 80/tcp comment "cn-home Traefik HTTP"
sudo ufw allow 443/tcp comment "cn-home Traefik HTTPS"
# Allow Prometheus (on a docker bridge) to scrape node-exporter on host net.
sudo ufw allow from 172.16.0.0/12 to any port 9100 proto tcp \
  comment "cn-observability node-exporter (docker bridges only)"
```

…or, if you prefer, disable UFW entirely (`sudo ufw disable`). Without these, pfSense's Unbound can't reach CoreDNS and Traefik can't satisfy step-ca's HTTP-01 challenge.

## Files

| Path | Purpose |
|---|---|
| `docker-compose.yml` | traefik-lan + dashy |
| `.env.example` | shape of the per-deploy `.env` |
| `setup.sh` | interactive `.env` generation + root-CA fetch + render templates |
| `deploy` | DNS + rsync + remote compose-up + verification |
| `domain.py` | pfSense Domain Override programmer (port of amun-kubernetes/domain.py) |
| `traefik-lan/dynamic.yml.tmpl` | routing contract (envsubst → `dynamic.yml`) |
| `dashy/conf.yml` | Dashy homepage config (YAML, hot-editable) |
| `certs/root_ca.crt` | fetched by setup.sh; mounted into Traefik for Lego trust (gitignored) |

## License

GNU GPL v3 © 2026 Gonzalo Alvarez
