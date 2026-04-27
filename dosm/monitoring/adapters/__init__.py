from __future__ import annotations

from dosm.monitoring.adapters.base import HostCheckResult, MonitoringAdapter
from dosm.monitoring.adapters.datadog import DatadogAdapter
from dosm.monitoring.adapters.dynatrace import DynatraceAdapter
from dosm.monitoring.adapters.servicenow import ServiceNowAdapter

TOOL_LABELS = {
    "dynatrace": "Dynatrace",
    "datadog": "Datadog",
    "servicenow": "ServiceNow",
}


def make_adapter(source, token: str, token2: str) -> MonitoringAdapter | None:
    if source.tool == "dynatrace":
        return DynatraceAdapter(source.id, source.name, source.url, token)
    if source.tool == "datadog":
        return DatadogAdapter(source.id, source.name, source.url, token, token2)
    if source.tool == "servicenow":
        return ServiceNowAdapter(
            source.id, source.name, source.url, source.username or "", token
        )
    return None


__all__ = [
    "HostCheckResult",
    "MonitoringAdapter",
    "DynatraceAdapter",
    "DatadogAdapter",
    "ServiceNowAdapter",
    "TOOL_LABELS",
    "make_adapter",
]
