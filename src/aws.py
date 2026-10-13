"""Shared AWS plumbing for the CloudWatch and CloudTrail clients: SSO login, a lazily created
boto3 client per profile, and AWS errors mapped to messages that say what to do."""
import os
import shutil
import subprocess
from typing import Iterable, List, Optional, Tuple

try:
    import boto3
    from botocore import exceptions as _bexc
    from botocore.exceptions import BotoCoreError, ClientError
    # Raised (depending on botocore version) when the SSO session is missing or expired.
    _AUTH_ERRORS = tuple(getattr(_bexc, n) for n in (
        "TokenRetrievalError", "SSOTokenLoadError", "UnauthorizedSSOTokenError", "NoCredentialsError",
    ) if hasattr(_bexc, n))
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False

SSO_LOGIN_TIMEOUT_SECONDS = 300  # time allowed to approve the login in the browser


class AwsError(Exception):
    pass


class AwsLoginError(AwsError):
    """The AWS SSO session is missing or expired; fixed by AWS Login (aws sso login)."""


def _aws_cli() -> Optional[str]:
    found = shutil.which("aws")
    if found:
        return found
    default = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Amazon", "AWSCLIV2", "aws.exe")
    return default if os.path.exists(default) else None


def sso_login(profile: str) -> Tuple[bool, str]:
    """Runs `aws sso login` for the profile. The CLI opens the browser to approve the login and
    returns once that's done (or it times out), so call this off the UI thread."""
    cli = _aws_cli()
    if not cli:
        return False, "The AWS CLI isn't installed (https://aws.amazon.com/cli/)."
    cmd = [cli, "sso", "login"] + (["--profile", profile] if profile else [])
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SSO_LOGIN_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flashing up
        )
    except subprocess.TimeoutExpired:
        return False, "AWS login timed out waiting for approval in the browser."
    except OSError as e:
        return False, f"Couldn't run the AWS CLI: {e}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, f"AWS login failed: {detail[-1] if detail else f'exit code {result.returncode}'}"
    return True, f"Logged in to AWS (profile '{profile}')."


def login_profiles(profiles: Iterable[str]) -> List[str]:
    """One profile per SSO session: profiles sharing a session are covered by one login, so this
    avoids a browser prompt per profile."""
    chosen, sessions = [], set()
    for profile in profiles:
        session = profile
        if HAS_BOTO3 and profile:
            try:
                cfg = boto3.Session(profile_name=profile)._session.full_config["profiles"].get(profile, {})
                session = cfg.get("sso_session") or cfg.get("sso_start_url") or profile
            except Exception:
                pass
        if session not in sessions:
            sessions.add(session)
            chosen.append(profile)
    return chosen


class AwsClient:
    """A boto3 client for one profile/region/service that's created on first use and dropped
    when the login expires, so the next call after AWS Login picks up fresh credentials."""
    service = ""
    label = "AWS"

    def __init__(self, profile: str, region: str):
        self.profile = profile
        self.region = region
        self._client = None
        self._session = None
        self.needs_login = False

    def _api(self):
        if not HAS_BOTO3:
            raise AwsError("boto3 is not installed. Run: pip install -r requirements.txt")
        if self._client is None:
            try:
                self._session = boto3.Session(profile_name=self.profile or None, region_name=self.region)
                self._client = self._session.client(self.service)
            except BotoCoreError as e:  # e.g. the profile doesn't exist in ~/.aws/config
                raise AwsError(str(e))
        return self._client

    def reset(self) -> None:
        """Drops the cached client so the next call loads credentials from a fresh login."""
        self._client = None
        self._session = None

    def _other(self, service: str):
        """Another service's client on the same session/credentials (e.g. STS)."""
        self._api()
        return self._session.client(service)

    def _login_error(self) -> AwsLoginError:
        self.reset()
        return AwsLoginError(f"AWS login for profile '{self.profile}' has expired. Click AWS Login.")

    def _client_error_message(self, code: str, error: Exception) -> str:
        """Subclasses turn service-specific error codes into a friendlier message."""
        if code == "AccessDeniedException":
            return f"Profile '{self.profile}' isn't allowed to call {self.service}."
        return str(error)

    def _call(self, fn, **kwargs):
        try:
            return fn(**kwargs)
        except _AUTH_ERRORS:
            raise self._login_error()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("ExpiredTokenException", "UnrecognizedClientException", "ForbiddenException"):
                raise self._login_error()
            raise AwsError(self._client_error_message(code, e))
        except BotoCoreError as e:
            raise AwsError(str(e))

    def _probe(self) -> str:
        """One cheap read that proves access; returns the success message."""
        raise NotImplementedError

    def test_connection(self) -> Tuple[bool, str]:
        """Confirms the AWS login works and the resource is readable. Sets needs_login when the
        failure is an expired/missing AWS login."""
        self.needs_login = False
        try:
            return True, f"{self.label}: {self._probe()}"
        except AwsError as e:
            self.needs_login = isinstance(e, AwsLoginError)
            return False, f"{self.label}: {e}"
