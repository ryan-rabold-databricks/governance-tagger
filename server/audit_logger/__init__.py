"""Reusable two-tier security audit logger for Databricks Apps.

Public API:

    from audit_logger import log_event, start, stop

    start()                          # call once at app startup
    log_event(                       # call from anywhere
        event_type="data_access",
        outcome="success",
        user_email="alice@corp.com",
        catalog_name="rtr_demo_catalog",
        schema_name="clinical",
        table_name="patient_enrollment",
        action="read_metadata",
    )
    stop()                           # call once at app shutdown

Configuration is via environment variables — see audit_logger.py for the full list.
"""

from .audit_logger import EventType, SecurityEvent, log_event, start, stop

__all__ = ["log_event", "start", "stop", "SecurityEvent", "EventType"]
