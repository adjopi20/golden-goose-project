from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from itertools import product
from typing import Iterable, Literal, Sequence


ExitMode = Literal["fixed", "trailing_only", "partial_trailing"]
TrailUnit = Literal["r", "atr", "percent"]


def _positive(name: str, value: float | None) -> None:
    if value is None or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def _number(value: float) -> str:
    return f"{value:g}".replace("-", "m").replace(".", "p")


@dataclass(frozen=True)
class ExitPolicy:
    """Strategy-independent instructions for managing an open position."""

    label: str
    mode: ExitMode
    target_r: float | None = None
    tp1_r: float | None = None
    tp1_fraction: float | None = None
    trail_activation_r: float | None = None
    trail_unit: TrailUnit | None = None
    trail_distance: float | None = None
    protection_floor_r: float | None = None

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("label must not be empty")
        if self.mode not in {"fixed", "trailing_only", "partial_trailing"}:
            raise ValueError(f"unknown exit mode: {self.mode}")
        if self.mode == "fixed":
            _positive("target_r", self.target_r)
            forbidden = (
                self.tp1_r,
                self.tp1_fraction,
                self.trail_activation_r,
                self.trail_unit,
                self.trail_distance,
                self.protection_floor_r,
            )
            if any(value is not None for value in forbidden):
                raise ValueError("fixed policy cannot contain partial or trailing fields")
            return

        _positive("trail_activation_r", self.trail_activation_r)
        _positive("trail_distance", self.trail_distance)
        if self.trail_unit not in {"r", "atr", "percent"}:
            raise ValueError("trailing policy requires trail_unit r, atr, or percent")
        if self.trail_unit == "percent" and float(self.trail_distance) > 100:
            raise ValueError("percentage trail distance cannot exceed 100")
        if self.target_r is not None:
            raise ValueError("trailing policy cannot contain a fixed target")
        if self.protection_floor_r is not None:
            if not math.isfinite(self.protection_floor_r):
                raise ValueError("protection_floor_r must be finite")
            if self.protection_floor_r < -1:
                raise ValueError("protection_floor_r cannot widen beyond the original -1R stop")
            if self.protection_floor_r > float(self.trail_activation_r):
                raise ValueError("protection_floor_r cannot exceed trail activation")

        if self.mode == "trailing_only":
            if self.tp1_r is not None or self.tp1_fraction is not None:
                raise ValueError("trailing_only policy cannot contain TP1 fields")
            return

        _positive("tp1_r", self.tp1_r)
        if (
            self.tp1_fraction is None
            or not math.isfinite(self.tp1_fraction)
            or not 0 < self.tp1_fraction < 1
        ):
            raise ValueError("tp1_fraction must be between zero and one")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def fixed_policy(target_r: float) -> ExitPolicy:
    return ExitPolicy(
        label=f"fixed_{_number(target_r)}r_full",
        mode="fixed",
        target_r=float(target_r),
    )


def trailing_policy(
    *,
    activation_r: float,
    unit: TrailUnit,
    distance: float,
    protection_floor_r: float | None,
) -> ExitPolicy:
    floor = "none" if protection_floor_r is None else f"{_number(protection_floor_r)}r"
    distance_label = (
        f"{_number(distance)}r" if unit == "r"
        else f"{_number(distance)}pct" if unit == "percent"
        else f"{_number(distance)}x"
    )
    return ExitPolicy(
        label=(
            f"trail_only_act_{_number(activation_r)}r_"
            f"{unit}_{distance_label}_floor_{floor}"
        ),
        mode="trailing_only",
        trail_activation_r=float(activation_r),
        trail_unit=unit,
        trail_distance=float(distance),
        protection_floor_r=protection_floor_r,
    )


def partial_trailing_policy(
    *,
    tp1_r: float,
    tp1_fraction: float,
    activation_r: float,
    unit: TrailUnit,
    distance: float,
    protection_floor_r: float | None,
) -> ExitPolicy:
    floor = "none" if protection_floor_r is None else f"{_number(protection_floor_r)}r"
    distance_label = (
        f"{_number(distance)}r" if unit == "r"
        else f"{_number(distance)}pct" if unit == "percent"
        else f"{_number(distance)}x"
    )
    return ExitPolicy(
        label=(
            f"tp1_{_number(tp1_r)}r_frac_{_number(tp1_fraction)}_"
            f"trail_act_{_number(activation_r)}r_{unit}_{distance_label}_floor_{floor}"
        ),
        mode="partial_trailing",
        tp1_r=float(tp1_r),
        tp1_fraction=float(tp1_fraction),
        trail_activation_r=float(activation_r),
        trail_unit=unit,
        trail_distance=float(distance),
        protection_floor_r=protection_floor_r,
    )


def build_exit_policy_grid(
    *,
    modes: Iterable[ExitMode],
    fixed_targets_r: Sequence[float] = (),
    tp1_r_values: Sequence[float] = (),
    tp1_fractions: Sequence[float] = (),
    trail_activation_r_values: Sequence[float] = (),
    risk_trail_distances_r: Sequence[float] = (),
    atr_trail_multipliers: Sequence[float] = (),
    percentage_trail_callbacks_pct: Sequence[float] = (),
    protection_floor_r_values: Sequence[float | None] = (None,),
) -> list[ExitPolicy]:
    """Build a deterministic Cartesian grid without evaluating market outcomes."""
    requested = tuple(dict.fromkeys(modes))
    unknown = set(requested) - {"fixed", "trailing_only", "partial_trailing"}
    if unknown:
        raise ValueError(f"Unknown exit modes: {sorted(unknown)}")

    trail_shapes: list[tuple[TrailUnit, float]] = [
        *(('r', float(value)) for value in risk_trail_distances_r),
        *(('atr', float(value)) for value in atr_trail_multipliers),
        *(('percent', float(value)) for value in percentage_trail_callbacks_pct),
    ]
    policies: list[ExitPolicy] = []
    if "fixed" in requested:
        policies.extend(fixed_policy(float(target)) for target in fixed_targets_r)
    if "trailing_only" in requested:
        policies.extend(
            trailing_policy(
                activation_r=float(activation),
                unit=unit,
                distance=distance,
                protection_floor_r=floor,
            )
            for activation, (unit, distance), floor in product(
                trail_activation_r_values,
                trail_shapes,
                protection_floor_r_values,
            )
            if floor is None or float(floor) <= float(activation)
        )
    if "partial_trailing" in requested:
        policies.extend(
            partial_trailing_policy(
                tp1_r=float(tp1),
                tp1_fraction=float(fraction),
                activation_r=float(activation),
                unit=unit,
                distance=distance,
                protection_floor_r=floor,
            )
            for tp1, fraction, activation, (unit, distance), floor in product(
                tp1_r_values,
                tp1_fractions,
                trail_activation_r_values,
                trail_shapes,
                protection_floor_r_values,
            )
            if floor is None or float(floor) <= float(activation)
        )

    labels = [policy.label for policy in policies]
    if not policies:
        raise ValueError("Exit grid is empty")
    if len(labels) != len(set(labels)):
        raise ValueError("Exit grid produced duplicate labels")
    return policies
