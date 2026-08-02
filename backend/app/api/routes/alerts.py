from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import IdentityContext, require_scope
from app.core.permissions import Scope
from app.observability.alerts import (
    AlertCooldown,
    AlertEvaluation,
    AlertRule,
    AlertState,
    alert_cooldown,
    evaluate_alerts,
)
from app.observability.health import collect_health
from app.observability.metrics import metrics

router = APIRouter(prefix="/api/v1/admin/alerts", tags=["alerts"])


class AlertStatePublic(BaseModel):
    rule: str
    severity: str
    status: str
    message: str
    value: float
    threshold: float


class AlertRulePublic(BaseModel):
    name: str
    severity: str
    description: str
    cause: str
    owner: str
    recovery: str


class AlertsPublic(BaseModel):
    fired: list[AlertStatePublic]
    resolved: list[AlertStatePublic]
    suppressed: int
    rules: list[AlertRulePublic]


def _state_public(state: AlertState) -> AlertStatePublic:
    return AlertStatePublic(**state.__dict__)


def _rule_public(rule: AlertRule) -> AlertRulePublic:
    return AlertRulePublic(**rule.__dict__)


@router.get("", response_model=AlertsPublic)
async def current_alerts(
    identity: IdentityContext = Depends(require_scope(Scope.USAGE_READ.value)),
) -> AlertsPublic:
    del identity
    evaluation: AlertEvaluation = evaluate_alerts(metrics.snapshot(), await collect_health())
    cooldown: AlertCooldown = alert_cooldown
    fired: list[AlertState] = []
    for state in evaluation.fired:
        if cooldown.allow(state.rule):
            fired.append(state)
            cooldown.mark_fired(state.rule)
    return AlertsPublic(
        fired=[_state_public(state) for state in fired],
        resolved=[_state_public(state) for state in evaluation.resolved],
        suppressed=len(evaluation.fired) - len(fired),
        rules=[_rule_public(rule) for rule in evaluation.rules],
    )
