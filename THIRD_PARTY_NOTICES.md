# Third-party software

The [MIT license](LICENSE) covers this repository's original source. It does not
relicense downloaded tools, Python packages, container images, or their contents.
Building the demo downloads these components under their respective terms.

Version and digest records are in `uv.lock`, `images/*/Dockerfile`,
`images/provisioner/*.txt`, the local Recipe lockfiles, and local image
declarations. Preserve upstream license files and notices when redistributing
an image or binary. Review the exact pinned distributions and their transitive
dependencies before publishing built artifacts.

Two dependencies have terms that differ from this repository's MIT license:

| Component | Applicable upstream terms |
|---|---|
| Terraform 1.15.8 | [Business Source License 1.1 and its Additional Use Grant](https://github.com/hashicorp/terraform/blob/v1.15.8/LICENSE). These include use restrictions and a later change license. |
| Redis 7.4 | [Redis licensing](https://redis.io/legal/licenses/). Redis 7.4 offers RSALv2 or SSPLv1; it is not the BSD-licensed Redis 7.2 line. |

Other distributions used by the demo include
[Radius](https://github.com/radius-project/radius),
[Bicep](https://github.com/Azure/bicep),
[Kubernetes](https://github.com/kubernetes/kubernetes),
[kind](https://github.com/kubernetes-sigs/kind),
[Azure CLI](https://github.com/Azure/azure-cli),
[kubelogin](https://github.com/Azure/kubelogin),
[uv](https://github.com/astral-sh/uv),
[Python](https://www.python.org/psf/license/), PostgreSQL, and Docker/base-image
packages. Their upstream distributions contain the applicable license and
copyright notices.

This document identifies licensing considerations; it is not a complete
software bill of materials or approval to redistribute every built image.
Public source availability does not imply that all dependencies are MIT-licensed
or that a hosted/commercial use is permitted by every dependency.
