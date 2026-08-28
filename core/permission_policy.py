from dataclasses import dataclass
from enum import Enum


class PermissionRisk(str, Enum):
    QUERY = "query"
    NORMAL_MUTATION = "normal_mutation"
    SENSITIVE_MUTATION = "sensitive_mutation"
    CATASTROPHIC = "catastrophic"


class PermissionAction(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionFacts:
    mode: str
    risk: PermissionRisk
    plan_internal: bool = False
    outside_workspace: bool = False
    sandbox_restricted: bool = True
    task_sandbox_bypass: bool = False
    task_grant_hit: bool = False
    project_rule_hit: bool = False


@dataclass(frozen=True)
class PermissionDecision:
    action: PermissionAction
    reason: str


def decide_permission(facts: PermissionFacts) -> PermissionDecision:
    if facts.risk is PermissionRisk.CATASTROPHIC:
        return PermissionDecision(PermissionAction.DENY, "catastrophic_denied")

    if (
        facts.mode == "plan"
        and not facts.plan_internal
        and facts.risk is not PermissionRisk.QUERY
    ):
        return PermissionDecision(PermissionAction.DENY, "plan_mutation_denied")

    if facts.plan_internal:
        return PermissionDecision(PermissionAction.ALLOW, "plan_internal_allow")

    if (
        facts.outside_workspace
        and facts.sandbox_restricted
        and not facts.task_sandbox_bypass
    ):
        return PermissionDecision(
            PermissionAction.ASK,
            "sandbox_boundary_requires_confirmation",
        )

    if facts.project_rule_hit:
        return PermissionDecision(PermissionAction.ALLOW, "project_rule_hit")

    if facts.task_grant_hit:
        return PermissionDecision(PermissionAction.ALLOW, "task_grant_hit")

    if facts.risk is PermissionRisk.QUERY:
        return PermissionDecision(PermissionAction.ALLOW, "query_allow")

    if facts.mode == "confirm":
        return PermissionDecision(
            PermissionAction.ASK,
            "confirm_requires_confirmation",
        )

    if facts.mode == "accept_edits":
        if facts.risk is PermissionRisk.SENSITIVE_MUTATION:
            return PermissionDecision(
                PermissionAction.ASK,
                "sensitive_requires_confirmation",
            )
        return PermissionDecision(PermissionAction.ALLOW, "normal_mutation_allow")

    if facts.mode == "full":
        return PermissionDecision(PermissionAction.ALLOW, "full_mode_allow")

    return PermissionDecision(
        PermissionAction.ASK,
        "unknown_mode_requires_confirmation",
    )
