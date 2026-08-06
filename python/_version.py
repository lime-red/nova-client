"""Single source of truth for the Python client's version.

Kept in step with the repo-root VERSION file and the $Script:NovaClientVersion
constant in the PowerShell clients. All three feed the User-Agent header so the
hub's logs identify which implementation a node is running.
"""

__version__ = "0.3.0"

USER_AGENT = f"nova-client-py/{__version__}"
