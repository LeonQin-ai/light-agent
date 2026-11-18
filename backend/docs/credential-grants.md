Strict-mode credential grants

In the chat composer, select 严格 → 凭据授权. Choose the operation and exact
destination, enter the secret in the password field, and insert the reference
into the task. To revoke it, open the same dialog in the issuing chat, paste
the reference and select 撤销授权. The reference is scoped to the runtime state
directory and chat. Fast-mode tasks cannot resolve it.

The adapter needs Python 3 with keyring installed in the interpreter environment
(python -m pip install keyring). PostgreSQL also needs psycopg
(python -m pip install "psycopg[binary]"). Set LIGHT_AGENT_CREDENTIAL_PYTHON
to the absolute interpreter path when using a virtual environment. Git clone
requires Git on PATH. Credentials use Windows Credential Manager, macOS
Keychain or a running Linux Secret Service. No plaintext-file fallback exists.

API grants authorize a Bearer GET to one HTTPS URL with no redirects. Git grants
authorize reference listing or cloning a single HTTPS repository into a fresh
workspace subdirectory. PostgreSQL grants authorize a TLS-verified login and
the SELECT entered when granting access. Without a query the tool returns the
database name. Responses are bounded and known credential echoes are masked.
