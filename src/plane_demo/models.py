from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from plane_demo.settings import TENANT_PATTERN

TenantId = Annotated[str, Field(pattern=f"^{TENANT_PATTERN}$", min_length=1, max_length=32)]


def validate_message(value: str) -> str:
    if "\x00" in value:
        raise ValueError("message cannot contain a null character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("message must contain valid Unicode") from None
    return value


Message = Annotated[str, Field(max_length=1024), AfterValidator(validate_message)]


class TenantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: TenantId
    isolation: Literal["shared", "isolated"]
    initial_message: Message


class ConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Message


class AppliedConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: TenantId
    onboarding_id: UUID
    message: Message
    version: int = Field(ge=1, le=9223372036854775807, strict=True)


@dataclass
class ReconcileResult:
    examined: int = 0
    succeeded: int = 0
    failed: int = 0
