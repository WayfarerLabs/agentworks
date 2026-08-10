"""GCE startup-script readiness constants for the shared stdin join."""

GCE_BOOTSTRAP_MARKER = "/var/lib/agentworks/gce-bootstrap-v1.complete"
GCE_READINESS_COMMAND = f"until test -f {GCE_BOOTSTRAP_MARKER}; do sleep 2; done"
GCE_READINESS_LABEL = "GCE startup script"
