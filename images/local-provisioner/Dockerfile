ARG API_IMAGE
FROM ${API_IMAGE}
USER root
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git openssl libicu72 && \
    rm -rf /var/lib/apt/lists/*
RUN case "$TARGETARCH" in \
      amd64) RAD_SHA=941daf7f646e93351a690f7327875e86adba761cf72d6e8238f1b6741798a745; \
        KUBE_SHA=12e97f9d23a9f6cbb87b89becd6bd291e1a858a3379a4e11e2c822c4c1530052; \
        BICEP_ARCH=x64; BICEP_SHA=aed90eb2c69a6ee2bd70dc0d4354408ac4d04fd9911d3ec8e0cd74ad173e7139 ;; \
      arm64) RAD_SHA=7de188b908157d19d6f0ab7a5736a2b3c083cb78416f98652c26b02f3a95a29e; \
        KUBE_SHA=5103d45b8881434d417694057b8ccb5ae79fd310a5c5c13e403c5e62e15909be; \
        BICEP_ARCH=arm64; BICEP_SHA=b01ac3bb5259096dfbe548138a538d1c4e4a55e6f87f3827e2299fbc2d4e6796 ;; \
      *) echo "Unsupported architecture" >&2; exit 1 ;; \
    esac && \
    mkdir -p /home/plane/.rad/bin /app/.state && \
    curl -fL --retry 3 "https://github.com/radius-project/radius/releases/download/v0.60.2/rad_linux_$TARGETARCH" -o /usr/local/bin/rad && \
    echo "$RAD_SHA  /usr/local/bin/rad" | sha256sum -c - && \
    curl -fL --retry 3 "https://dl.k8s.io/release/v1.35.7/bin/linux/$TARGETARCH/kubectl" -o /usr/local/bin/kubectl && \
    echo "$KUBE_SHA  /usr/local/bin/kubectl" | sha256sum -c - && \
    curl -fL --retry 3 "https://github.com/Azure/bicep/releases/download/v0.42.1/bicep-linux-$BICEP_ARCH" -o /home/plane/.rad/bin/bicep && \
    echo "$BICEP_SHA  /home/plane/.rad/bin/bicep" | sha256sum -c - && \
    chmod 0755 /usr/local/bin/rad /usr/local/bin/kubectl /home/plane/.rad/bin/bicep && \
    chown -R 10001:10001 /home/plane /app/.state
COPY src/plane_demo/management/provisioner.py src/plane_demo/management/provisioning.py /app/src/plane_demo/management/
COPY src/plane_demo/management/providers /app/src/plane_demo/management/providers
COPY infra/radius/apps/*.bicep /app/infra/radius/apps/
COPY infra/radius/modules/*.bicep /app/infra/radius/modules/
COPY infra/radius/environments/*.bicep /app/infra/radius/environments/
COPY infra/radius/types/*.yaml infra/radius/types/*.tgz /app/infra/radius/types/
COPY infra/radius/bicepconfig.json /app/infra/radius/
COPY infra/radius/recipes /app/infra/radius/recipes
COPY scripts/__init__.py /app/scripts/
COPY scripts/operations/__init__.py scripts/operations/config.py scripts/operations/demo.py \
    scripts/operations/project.py scripts/operations/install-radius.py scripts/operations/deploy-plane.py \
    scripts/operations/register-radius.py scripts/operations/issue-certificate.py scripts/operations/acme-hook.py \
    scripts/operations/run-certificate-job.py /app/scripts/operations/
COPY scripts/operations/local /app/scripts/operations/local
COPY scripts/recipes/local/cluster/*.sh /app/scripts/recipes/local/cluster/
ENV HOME=/home/plane
USER 10001:10001
RUN /home/plane/.rad/bin/bicep --version && rad version --cli && kubectl version --client && \
    uv pip check --python /app/.venv/bin/python
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=$SOURCE_REVISION
CMD ["python", "-m", "plane_demo.management.provisioner"]
