"""Authorize the Drive TEST integration as a COMPANY USER (Google OAuth 2.0, installed-app loopback flow with PKCE).

Run it with the project's own interpreter:

    & "D:\\Bills\\backend\\.venv\\Scripts\\python.exe" backend\\drive_spike\\oauth_authorize.py --check
    & "D:\\Bills\\backend\\.venv\\Scripts\\python.exe" backend\\drive_spike\\oauth_authorize.py --authorize

  --check       contacts NOTHING. Reports which files exist, whether the token file's permissions are acceptable and whether it matches the
                OAuth client and scope. Prints no secret.
  --authorize   opens Google's consent page in your browser, receives the answer on 127.0.0.1 (random port), exchanges it and writes ONLY the
                refresh-token file (refused if one already exists - it is never overwritten). It does not call the Drive API.

Paths come from --client-file / --token-file or SPINCO_GDRIVE_OAUTH_CLIENT_FILE / SPINCO_GDRIVE_OAUTH_TOKEN_FILE. Both must be OUTSIDE the repository.
The scope defaults to SPINCO_GDRIVE_SCOPE, else the full `drive` scope (see docs: `drive.file` cannot see a folder this app did not create).
"""

import argparse
import json
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_PYTHON = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"


def _wrong_interpreter() -> str | None:
    try:
        import httpx  # noqa: F401
    except ImportError:
        return f"This interpreter ({sys.executable}) lacks httpx. Run with {PROJECT_PYTHON}; nothing needs installing."
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Authorize the Spinco TA Audit Drive test integration as a company user.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="offline pre-flight check; contacts nothing")
    mode.add_argument("--authorize", action="store_true", help="run the browser sign-in and write the token file")
    parser.add_argument("--client-file", help="the Desktop-client JSON (default: SPINCO_GDRIVE_OAUTH_CLIENT_FILE)")
    parser.add_argument("--token-file", help="where the refresh token is stored (default: SPINCO_GDRIVE_OAUTH_TOKEN_FILE)")
    parser.add_argument("--scope", help="OAuth scope (default: SPINCO_GDRIVE_SCOPE, else the full drive scope)")
    parser.add_argument("--login-hint", help="pre-select this Google account on the consent page")
    parser.add_argument("--no-browser", action="store_true", help="only print the URL; open it yourself")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for the sign-in (default 300)")
    args = parser.parse_args(argv)

    problem = _wrong_interpreter()
    if problem:
        print(f"BLOCKED: {problem}")
        return 4
    sys.path.insert(0, str(BACKEND_DIR))
    from app.services.drive import config as drive_config
    from app.services.drive.auth import DriveAuthError
    from app.services.drive.oauth import OAuthClientConfig, OAuthTokenStore, authorize_interactively, inspect_token_file, permission_problem

    raw_client = args.client_file or os.environ.get(drive_config.OAUTH_CLIENT_FILE_ENV, "")
    raw_token = args.token_file or os.environ.get(drive_config.OAUTH_TOKEN_FILE_ENV, "")
    scope = (args.scope or os.environ.get(drive_config.SCOPE_ENV) or drive_config.DEFAULT_SCOPE).strip()
    try:
        if not raw_client or not raw_token:
            raise drive_config.DriveConfigError(f"Give --client-file and --token-file, or set {drive_config.OAUTH_CLIENT_FILE_ENV} and {drive_config.OAUTH_TOKEN_FILE_ENV}.")
        client_path = drive_config._outside_repo(raw_client, "The OAuth client file must be OUTSIDE the repository.")
        token_path = drive_config._outside_repo(raw_token, "The OAuth token file must be OUTSIDE the repository.")
    except drive_config.DriveConfigError as exc:
        print(f"CONFIGURATION: {exc}")
        return 2
    store = OAuthTokenStore(token_path)

    if args.check:
        report = {"scope": scope, "client_file": {"path": str(client_path), "exists": client_path.is_file()}, "token_file": {"path": str(token_path), "folder_exists": token_path.parent.is_dir()}}
        client = None
        if client_path.is_file():
            try:
                client = OAuthClientConfig.from_file(client_path)
                report["client_file"].update(valid=True)  # nothing from inside the file is ever echoed, not even the client id
            except DriveAuthError as exc:
                report["client_file"].update(valid=False, problem=str(exc))
            acl = permission_problem(client_path, "the OAuth client file")  # advisory only: the client file is never modified by this tool
            report["client_file"]["permissions_note"] = acl or "only the current user (and SYSTEM / Administrators) can read it"
        report["token_file"].update(inspect_token_file(store, client, scope))
        print(json.dumps(report, indent=2))
        ready = bool(report["token_file"].get("valid") and report["token_file"].get("client_matches") and report["token_file"].get("scope_ok"))
        print("Token file ready for use." if ready else "Not authorized yet: run this script with --authorize.")
        return 0

    try:
        client = OAuthClientConfig.from_file(client_path)
        print(f"Requested scope: {scope}")
        result = authorize_interactively(
            client,
            store,
            scope,
            open_browser=(lambda url: False) if args.no_browser else __import__("webbrowser").open,
            announce=lambda url: print("Open this address in your browser and sign in with the COMPANY account (the address itself holds no credential):\n\n" + url + "\n\nWaiting for the sign-in..."),
            timeout=args.timeout,
            login_hint=args.login_hint,
        )
    except DriveAuthError as exc:
        print(f"NOT AUTHORIZED: {exc}")
        return 1
    print(f"Authorized. Granted scope: {result.scope}\nToken file: {result.token_file} (access restricted to the current Windows user)\nNo Google Drive operation was performed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
