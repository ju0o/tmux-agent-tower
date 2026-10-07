"""Synthetic remote paths for public regression tests.

These strings are protocol fixtures. They are not this machine, not an
workstation-b home directory, and the tests that use them do not open SSH.
"""

REMOTE_HOME = "/remote/agent-host"
REMOTE_PROJECTS = "/remote/agent-host/projects"
REMOTE_GROUP = "/remote/agent-host/projects/sample-group"
REMOTE_CODE = "/remote/agent-host/projects/sample-group/code"
REMOTE_REPO = "/remote/agent-host/projects/sample-repo"
REMOTE_NOTES = "/remote/agent-host/notes"
REMOTE_MISSING = "/remote/agent-host/projects/missing"
REMOTE_OTHER = "/remote/agent-host/projects/other"
