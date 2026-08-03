# The four Secrets this deployment expects

They are created with `kubectl`, not generated from a file in this repository.
`secretGenerator` would work and is deliberately not used: `SESSION_JWT_SECRET`
signs every credential in the system, and §7's position is that it exists in
exactly two services and nowhere else. A file in the repository is a third place,
whether or not it is gitignored.

All four live in the `agrarian` namespace.

## 1. `agrarian-session-jwt` — the only shared secret

Carried by **db-writer** (which mints) and **ws-server** (which validates), and by
nothing else. The portal does not get it and must not: it holds the session cookie,
forwards its value, and lets db-writer answer 401. That is one extra hop per
request, and it buys the signing key being absent from the tier facing the internet.

```sh
kubectl -n agrarian create secret generic agrarian-session-jwt \
  --from-literal=SESSION_JWT_SECRET="$(openssl rand -hex 32)"
```

Rotating it invalidates every session, viewer and publisher token at once. A flight
in the air keeps publishing — its container already holds a valid token and nothing
re-checks it until expiry — but no new viewer can be authorised until the browser
signs in again.

## 2. `agrarian-db` — the database worker credentials

A managed PostgreSQL is outside the cluster, so `DB_HOST` is its endpoint rather
than a Service in this namespace.

```sh
kubectl -n agrarian create secret generic agrarian-db \
  --from-literal=DB_HOST=agrarian.postgres.database.azure.com \
  --from-literal=DB_NAME=agrarian \
  --from-literal=DB_WORKER_NAME=agrarian_worker \
  --from-literal=DB_WORKER_PASSWORD='...'
```

Least privilege, per §7: this account needs DML on the five tables and nothing
else. It should not own the schema — `rebuild_schema.py` is destructive by design
and is run by a person with different credentials.

## 3. `agrarian-recording-store` — object storage for recordings

Mounted by the recorder sidecar only. `local` keeps segments on the `recordings`
claim and uploads nothing, which is a development mode: the claim is sized for a
backlog, not for an archive.

```sh
kubectl -n agrarian create secret generic agrarian-recording-store \
  --from-literal=RECORDING_STORE_SERVICE=azure \
  --from-literal=RECORDING_DELETE_LOCAL_ON_SUCCESS=true \
  --from-literal=RECORDING_AZURE_CONNECTION_STRING='...' \
  --from-literal=RECORDING_AZURE_CONTAINER_NAME=recordings \
  --from-literal=RECORDING_AZURE_BLOB_PREFIX=
```

The Azure and AWS backends have never been verified against real credentials (§9),
so the first deployment that sets either should check that a segment actually
arrives rather than assuming the 200 means it did.

## 4. `agrarian-tls` — the leaf all three terminators read

**This one is normally not created by hand.** cert-manager writes it, and the three
services that mount it — Traefik, MediaMTX and Mosquitto — each read a certificate
and a key from disk without asking who signed it. That property is what let the
whole ingress tier be built and measured against a local CA before any hostname
existed.

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: agrarian-tls
  namespace: agrarian
spec:
  secretName: agrarian-tls          # the name the three Deployments mount
  dnsNames:
    - agrarian.example.com
    - "*.agrarian.example.com"      # a wildcard, which is why DNS-01 is needed
  issuerRef:
    name: letsencrypt
    kind: ClusterIssuer
```

Until a hostname resolves here, `scripts/generate_local_certs.sh` stands in and the
Secret is created from its output:

```sh
kubectl -n agrarian create secret generic agrarian-tls \
  --from-file=tls.crt=certificates/server/server.crt \
  --from-file=tls.key=certificates/server/server.key \
  --from-file=ca.crt=certificates/ca/ca.crt
```

Two renewal notes, both measured in `tests/comms/run_cert_renewal.sh`:

- **MediaMTX rereads the file by itself**, within seconds, and a flight already in
  the air is undisturbed. Do **not** send it `SIGHUP` by analogy with Mosquitto —
  that kills the process, and with it every flight in the air.
- **Mosquitto needs `SIGHUP`.** Nothing in these manifests sends one, so that is
  still a job for whatever drives renewal. It is the one piece of the renewal
  question this platform does not answer on its own.

Traefik's caveat *does* go away here: under compose it never notices a replaced
leaf, because `watch: true` watches the routing directory and the certificate is
mounted outside it. A Secret volume is refreshed in place by the kubelet, and
Traefik's watcher sees it.
