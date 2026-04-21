"""System service management for Kyber's gateway and dashboard.

Provides install/uninstall/status helpers that write launchd plists on
macOS and systemd user units on Linux. Used by the ``kyber service``
CLI command group so users can enable background mode after the initial
install.
"""

from kyber.service.manager import (
    ServiceInfo,
    UnitStatus,
    UnsupportedPlatformError,
    ensure_port_free,
    install_services,
    kill_orphan_kyber_processes,
    restart_services,
    running_under_service_manager,
    service_status,
    uninstall_services,
)

__all__ = [
    "ServiceInfo",
    "UnitStatus",
    "UnsupportedPlatformError",
    "ensure_port_free",
    "install_services",
    "kill_orphan_kyber_processes",
    "restart_services",
    "running_under_service_manager",
    "service_status",
    "uninstall_services",
]
