from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, model_validator


class TravelBudgetCalculatorInput(BaseModel):
    """Input schema for travel budget calculation."""

    total_budget: float = Field(..., gt=0, description="Total trip budget in selected currency")
    trip_days: int = Field(..., gt=0, description="Number of travel days")
    accommodation_ratio: float = Field(..., ge=0, le=1)
    food_ratio: float = Field(..., ge=0, le=1)
    transport_ratio: float = Field(..., ge=0, le=1)
    activities_ratio: float = Field(..., ge=0, le=1)
    contingency_ratio: float = Field(default=0.1, ge=0, le=1)

    @model_validator(mode="after")
    def validate_ratios(self) -> "TravelBudgetCalculatorInput":
        ratio_sum = (
            self.accommodation_ratio
            + self.food_ratio
            + self.transport_ratio
            + self.activities_ratio
            + self.contingency_ratio
        )
        if ratio_sum > 1.0:
            raise ValueError(f"Cost ratios exceed 1.0 ({ratio_sum:.2f}). Reduce one or more ratios.")
        return self


class TravelBudgetCalculatorTool(BaseTool):
    name: str = "travel_budget_calculator"
    description: str = (
        "Calculate category-wise travel budget allocations and daily spending from budget ratios. "
        "Use this for all arithmetic in budget planning."
    )
    args_schema: Type[BaseModel] = TravelBudgetCalculatorInput

    def _run(
        self,
        total_budget: float,
        trip_days: int,
        accommodation_ratio: float,
        food_ratio: float,
        transport_ratio: float,
        activities_ratio: float,
        contingency_ratio: float = 0.1,
    ) -> str:
        accommodation = total_budget * accommodation_ratio
        food = total_budget * food_ratio
        transport = total_budget * transport_ratio
        activities = total_budget * activities_ratio
        contingency = total_budget * contingency_ratio
        allocated = accommodation + food + transport + activities + contingency
        unallocated = total_budget - allocated

        safe_divisor = trip_days if trip_days > 0 else 1

        return (
            "{"
            f"\"trip_days\": {trip_days},"
            f" \"total_budget\": {total_budget:.2f},"
            f" \"accommodation\": {accommodation:.2f},"
            f" \"food\": {food:.2f},"
            f" \"transport\": {transport:.2f},"
            f" \"activities\": {activities:.2f},"
            f" \"contingency\": {contingency:.2f},"
            f" \"allocated_total\": {allocated:.2f},"
            f" \"unallocated\": {unallocated:.2f},"
            f" \"daily_average\": {(allocated / safe_divisor):.2f}"
            "}"
        )
