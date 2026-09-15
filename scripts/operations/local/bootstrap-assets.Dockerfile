ARG EXECUTOR_BASE
ARG OPERATOR_BASE
ARG PROVISIONER_BASE
ARG TOOLS_BASE

FROM ${TOOLS_BASE} AS tools

FROM ${EXECUTOR_BASE} AS executor
ARG TARGETARCH
USER 0:0
COPY infra/radius/recipes/local /recipe-sources
RUN mkdir -p /opt/radplanes/providers && \
    for recipe in cluster postgresql redis gateway; do \
      TF_CLI_CONFIG_FILE=/dev/null /opt/radplanes/terraform \
        -chdir="/recipe-sources/$recipe" providers mirror \
        -platform="linux_$TARGETARCH" /opt/radplanes/providers; \
    done && \
    chmod -R a+rX /opt/radplanes/providers
COPY scripts/operations/local/terraform.tfrc /opt/radplanes/terraform.tfrc
COPY --from=tools /tmp/terraform.zip /opt/radplanes/terraform.zip
COPY scripts/operations/local/.packaged /opt/radplanes/bootstrap
# Only this non-secret artifact tree is shared by the two runtime UIDs.
RUN find /opt/radplanes -type d -exec chmod 0755 '{}' + && \
    find /opt/radplanes -type f -exec chmod 0644 '{}' + && \
    chmod 0755 /opt/radplanes/terraform
ENV TF_CLI_CONFIG_FILE=/opt/radplanes/terraform.tfrc
USER 65532:65532

FROM ${OPERATOR_BASE} AS operator
COPY --from=executor /opt/radplanes /opt/radplanes

FROM ${PROVISIONER_BASE} AS provisioner
COPY --from=executor /opt/radplanes /opt/radplanes
