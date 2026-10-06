"""The six approaches, labelled exactly as in DESIGN.md section 3.

One source for the labels and for how each approach is deployed. The pilot
arms (b_spec, d_boundary) are not approaches and cannot be run from here.
"""
from dataclasses import dataclass

IDS: tuple[int, ...] = (1, 2, 3, 4, 5, 6)
GENERATIONS: tuple[int, ...] = (1, 2, 3, 4, 5)   # approach 3 only (DESIGN section 4)
GATEWAY_APPROACHES: frozenset[int] = frozenset({4, 5, 6})


@dataclass(frozen=True)
class Approach:
    id: int
    label: str
    agent_module: str          # module whose run() drives the agent
    gateway_plugin: str | None  # GATEWAY_PLUGIN value for approaches 4 to 6

    @property
    def gateway(self) -> bool:
        return self.gateway_plugin is not None


APPROACHES: dict[int, Approach] = {
    1: Approach(1, "Instructions to the agent", "arms.a_guides.agent", None),
    2: Approach(2, "Checkpoint inside the agent, written by hand", "arms.c_hook.agent", None),
    3: Approach(3, "Checkpoint inside the agent, generated from a spec", "arms.approach3.agent", None),
    4: Approach(4, "Policy at a gateway", "arms.a_guides.agent", "approach4"),
    5: Approach(5, "Gateway with shared central policy, called per decision",
                "arms.a_guides.agent", "approach5"),
    6: Approach(6, "Gateway with shared central policy, evaluated locally",
                "arms.a_guides.agent", "approach6"),
}


def label(approach: int) -> str:
    return APPROACHES[approach].label


def get(approach: int) -> Approach:
    if approach not in APPROACHES:
        raise ValueError(f"approach {approach!r} is not runnable; use one of {IDS}")
    return APPROACHES[approach]
