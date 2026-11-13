"""Application-level models shared across modules. The UI depends on these, never on raw NetBox JSON."""

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class Ref(ApiModel):
    """A reference to a related NetBox object."""

    id: int
    name: str
    slug: str | None = None


class Choice(ApiModel):
    value: str
    label: str


class Page(ApiModel, Generic[T]):
    count: int
    limit: int
    offset: int
    results: list[T]


class Tag(ApiModel):
    id: int
    name: str
    slug: str
    color: str | None = None


class Meta(ApiModel):
    """Bookkeeping shared by NetBox-backed objects."""

    created: datetime | None = None
    last_updated: datetime | None = None
    netbox_url: str | None = Field(default=None, description="Link to the object in the NetBox UI")


class Message(ApiModel):
    message: str


def ref(obj: dict[str, Any] | None) -> Ref | None:
    if not obj:
        return None
    name = obj.get("name") or obj.get("display") or str(obj.get("id"))
    return Ref(id=int(obj["id"]), name=str(name), slug=obj.get("slug"))


def choice(obj: dict[str, Any] | str | None) -> Choice | None:
    if obj is None or obj == "":
        return None
    if isinstance(obj, str):
        return Choice(value=obj, label=obj.replace("_", " ").title())
    value = obj.get("value")
    if value in (None, ""):
        return None
    return Choice(value=str(value), label=str(obj.get("label") or value))


def tags(items: list[dict[str, Any]] | None) -> list[Tag]:
    return [
        Tag(
            id=int(t["id"]),
            name=str(t.get("name") or t.get("display")),
            slug=str(t.get("slug")),
            color=t.get("color"),
        )
        for t in items or []
        if "id" in t
    ]


def meta(obj: dict[str, Any]) -> Meta:
    from lanvex.core.config import get_settings

    return Meta(
        created=obj.get("created"),
        last_updated=obj.get("last_updated"),
        netbox_url=get_settings().public_netbox_url(obj.get("display_url")),
    )
