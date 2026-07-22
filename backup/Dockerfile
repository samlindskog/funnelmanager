# Leads backup runner: kubectl (to exec mongodump inside the mongo pod) + mc
# (MinIO client, S3-compatible upload to Linode Object Storage). Both baked in
# at build time — no runtime downloads. Runs as non-root.
FROM alpine:3.20

ARG KUBECTL_VERSION=v1.31.5
RUN apk add --no-cache ca-certificates bash curl \
 && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl \
 && curl -fsSL "https://dl.min.io/client/mc/release/linux-amd64/mc" -o /usr/local/bin/mc \
 && chmod +x /usr/local/bin/kubectl /usr/local/bin/mc \
 && apk del curl

COPY backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/backup.sh

USER 10001
ENTRYPOINT ["/usr/local/bin/backup.sh"]
