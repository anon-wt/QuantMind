"""One immutable GP parameter contract for preview, execution and audit."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    population_size: int = Field(48, ge=8, le=128, strict=True)
    generations: int = Field(8, ge=1, le=30, strict=True)
    top_k: int = Field(5, ge=1, le=16, strict=True)
    tournament_size: int = Field(3, ge=2, le=8, strict=True)
    elite_size: int = Field(2, ge=1, le=8, strict=True)
    max_depth: int = Field(4, ge=2, le=6, strict=True)
    max_nodes: int = Field(31, ge=3, le=63, strict=True)
    windows: list[Annotated[int, Field(strict=True, ge=1, le=60)]] = Field(
        default_factory=lambda: [5, 10, 20], min_length=1, max_length=8
    )
    crossover_rate: float = Field(0.7, ge=0, le=1)
    mutation_rate: float = Field(0.25, ge=0, le=1)
    complexity_penalty: float = Field(0.0001, ge=0, le=0.01)
    seed: int = Field(42, ge=0, le=2**31 - 1, strict=True)
    horizon: int = Field(1, ge=1, le=250, strict=True)
    min_names: int = Field(10, ge=3, le=10000, strict=True)
    min_days: int = Field(40, ge=5, le=5000, strict=True)
    dedup_corr: float = Field(0.95, gt=0, le=1)

    @model_validator(mode="after")
    def bounded_search(self):
        if self.population_size * self.generations > 2048:
            raise ValueError("GP 种群规模 × 代数不能超过 2048")
        if self.elite_size >= self.population_size or self.top_k > self.population_size:
            raise ValueError("GP 精英数须小于种群规模，Top-K 不能超过种群规模")
        if len(set(self.windows)) != len(self.windows) or any(
            type(w) is not int or not 1 <= w <= 60 for w in self.windows
        ):
            raise ValueError("GP 窗口须为互不重复的 1～60 交易日整数")
        return self
