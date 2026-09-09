from plane_demo.management.provisioning import ProvisioningError


def unsupported() -> None:
    raise ProvisioningError("local_integration_gate_not_passed")
