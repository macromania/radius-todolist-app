FROM docker.io/library/docker:29.2.1-cli@sha256:cab69e2d0a1a2ea9a1ce1060252f439e83483ae41ec09317aecb33b08a0656a5 AS tools
ARG TARGETARCH
RUN case "$TARGETARCH" in \
      amd64) TF_SHA=d25ce7b6902013ad905db3d2eab0be4cd905887fe88b81a6171b8d5503c31f3d; \
        RAD_SHA=941daf7f646e93351a690f7327875e86adba761cf72d6e8238f1b6741798a745; \
        KUBE_SHA=12e97f9d23a9f6cbb87b89becd6bd291e1a858a3379a4e11e2c822c4c1530052 ;; \
      arm64) TF_SHA=8891e9dcedc9e3b8950bc6af9d4d8af1f4cfade3062f53b9dc403a89f6ce8c9c; \
        RAD_SHA=7de188b908157d19d6f0ab7a5736a2b3c083cb78416f98652c26b02f3a95a29e; \
        KUBE_SHA=5103d45b8881434d417694057b8ccb5ae79fd310a5c5c13e403c5e62e15909be ;; \
      *) echo "Unsupported architecture" >&2; exit 1 ;; \
    esac && \
    mkdir /tools && \
    wget -O /tmp/terraform.zip "https://releases.hashicorp.com/terraform/1.15.8/terraform_1.15.8_linux_$TARGETARCH.zip" && \
    echo "$TF_SHA  /tmp/terraform.zip" | sha256sum -c - && \
    unzip /tmp/terraform.zip -d /tools && \
    wget -O /tools/rad "https://github.com/radius-project/radius/releases/download/v0.60.2/rad_linux_$TARGETARCH" && \
    echo "$RAD_SHA  /tools/rad" | sha256sum -c - && \
    wget -O /tools/kubectl "https://dl.k8s.io/release/v1.35.7/bin/linux/$TARGETARCH/kubectl" && \
    echo "$KUBE_SHA  /tools/kubectl" | sha256sum -c - && \
    chmod 0755 /tools/terraform /tools/rad /tools/kubectl

FROM ghcr.io/radius-project/dynamic-rp:0.60@sha256:225dcb42382cc8f83fa1eda22e2a193fce9a76428af39cb67e9033172ecd4783 AS executor
COPY --from=tools /usr/local/bin/docker /usr/local/bin/docker
COPY --from=tools /tools/terraform /opt/radplanes/terraform
USER 65532:65532
RUN docker --version && /opt/radplanes/terraform version && \
    test -x /dynamic-rp && git --version && test -s /etc/ssl/certs/ca-certificates.crt
# Inherit the upstream /dynamic-rp entrypoint, working directory, and binary.

FROM docker.io/library/python:3.13.12-alpine3.23@sha256:bb1f2fdb1065c85468775c9d680dcd344f6442a2d1181ef7916b60a623f11d40 AS operator
COPY --from=tools /tools/rad /tools/kubectl /usr/local/bin/
RUN mkdir -p /home/operator && chown 65532:65532 /home/operator
ENV HOME=/home/operator
USER 65532:65532
RUN rad version --cli && kubectl version --client
ENTRYPOINT ["python3"]
CMD []
