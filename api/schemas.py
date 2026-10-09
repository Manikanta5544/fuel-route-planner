from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


Place = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class RouteRequest(BaseModel):
    """Defaults are the assignment's vehicle; mpg and a *shorter* range are for what-if runs."""

    model_config = ConfigDict(extra="forbid")
    start: Point | Place
    finish: Point | Place
    mpg: float = Field(default=10.0, ge=1, le=100)
    max_range_miles: float = Field(default=500.0, ge=50, le=500)
