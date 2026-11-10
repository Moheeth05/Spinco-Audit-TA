"""Workload Identity Federation (external_account) sign-in. FAKE transport (httpx.MockTransport): these tests prove OUR token-exchange logic, the endpoint allow-list and the
secret hygiene. They are NOT evidence about Google's real STS / IAM Credentials services - that exchange must be verified once with the company's identity provider in staging."""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.services.drive import config as drive_config
from app.services.drive.auth import DriveAuthPermanentError, DriveAuthTransientError, TokenSettings
from app.services.drive.config import DriveConfigError
from app.services.drive.runtime import build_token_provider
from app.services.drive.wif import ExternalAccountConfig, ExternalAccountTokenProvider

AUDIENCE = "//iam.googleapis.com/projects/123456789/locations/global/workloadIdentityPools/spinco-pool/providers/entra"
STS = "https://sts.googleapis.com/v1/token"
IAM = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/spinco-drive@spinco-prod.iam.gserviceaccount.com:generateAccessToken"
SUBJECT = "SYNTHETIC-SUBJECT-TOKEN-0123456789"
FEDERATED = "SYNTHETIC-FEDERATED-TOKEN-ABCDEF"
ACCESS = "SYNTHETIC-DRIVE-ACCESS-TOKEN-XYZ"


def write_config(tmp_path, *, subject_text=SUBJECT, impersonate=True, **overrides):
    token_file = tmp_path / "subject-token.txt"
    token_file.write_text(subject_text, encoding="utf-8")
    data = {
        "type": "external_account",
        "audience": AUDIENCE,
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": STS,
        "credential_source": {"file": str(token_file)},
    }
    if impersonate:
        data["service_account_impersonation_url"] = IAM
    data.update(overrides)
    path = tmp_path / "wif.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, token_file


class Recorder:
    def __init__(self, sts_status=200, iam_status=200, sts_body=None, iam_body=None):
        self.requests: list[httpx.Request] = []
        self.sts_status, self.iam_status = sts_status, iam_status
        self.sts_body = sts_body if sts_body is not None else {"access_token": FEDERATED, "expires_in": 3599, "token_type": "Bearer"}
        self.iam_body = iam_body if iam_body is not None else {"accessToken": ACCESS, "expireTime": "2099-01-01T00:00:00Z"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == STS:
            return httpx.Response(self.sts_status, json=self.sts_body)
        if str(request.url) == IAM:
            return httpx.Response(self.iam_status, json=self.iam_body)
        return httpx.Response(404, json={"error": "unexpected url"})


def provider(config_path, recorder, scope="https://www.googleapis.com/auth/drive", clock=lambda: 1_700_000_000.0):
    http = httpx.Client(transport=httpx.MockTransport(recorder))
    return ExternalAccountTokenProvider(ExternalAccountConfig.from_file(config_path), scope, http, clock=clock, settings=TokenSettings())


def test_full_exchange_with_impersonation_returns_the_drive_token(tmp_path):
    path, _ = write_config(tmp_path)
    rec = Recorder()
    token = provider(path, rec).get_token()
    assert token == ACCESS
    assert [str(r.url) for r in rec.requests] == [STS, IAM]
    form = parse_qs(rec.requests[0].content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:token-exchange"]
    assert form["audience"] == [AUDIENCE]
    assert form["subject_token"] == [SUBJECT]
    assert form["subject_token_type"] == ["urn:ietf:params:oauth:token-type:jwt"]
    assert form["scope"] == ["https://www.googleapis.com/auth/cloud-platform"]  # the broad scope is only for the impersonation call
    assert rec.requests[1].headers["authorization"] == f"Bearer {FEDERATED}"
    assert json.loads(rec.requests[1].content) == {"scope": ["https://www.googleapis.com/auth/drive"], "lifetime": "3600s"}


def test_without_impersonation_the_federated_token_itself_is_used_with_the_drive_scope(tmp_path):
    path, _ = write_config(tmp_path, impersonate=False)
    rec = Recorder()
    assert provider(path, rec).get_token() == FEDERATED
    assert [str(r.url) for r in rec.requests] == [STS]
    assert parse_qs(rec.requests[0].content.decode())["scope"] == ["https://www.googleapis.com/auth/drive"]


def test_the_token_is_cached_until_it_nears_expiry_and_the_subject_token_file_is_reread_on_refresh(tmp_path):
    path, token_file = write_config(tmp_path)
    now = [1_700_000_000.0]
    rec = Recorder(iam_body={"accessToken": ACCESS, "expireTime": "2023-11-14T23:13:20Z"})  # 1_700_000_000 + 3600 s
    p = provider(path, rec, clock=lambda: now[0])
    assert p.get_token() == ACCESS
    assert p.get_token() == ACCESS
    assert len(rec.requests) == 2  # one exchange (STS + IAM), the second call was served from the cache
    token_file.write_text("SECOND-SUBJECT-TOKEN", encoding="utf-8")  # the identity provider's refresher wrote a new one
    now[0] += 3600
    p.get_token()
    assert parse_qs(rec.requests[2].content.decode())["subject_token"] == ["SECOND-SUBJECT-TOKEN"]


def test_json_formatted_subject_token_files_are_supported(tmp_path):
    token_file = tmp_path / "subject.json"
    token_file.write_text(json.dumps({"id_token": SUBJECT, "other": 1}), encoding="utf-8")
    path, _ = write_config(tmp_path, credential_source={"file": str(token_file), "format": {"type": "json", "subject_token_field_name": "id_token"}})
    rec = Recorder()
    provider(path, rec).get_token()
    assert parse_qs(rec.requests[0].content.decode())["subject_token"] == [SUBJECT]


@pytest.mark.parametrize(
    "overrides",
    [
        {"token_url": "https://evil.example.com/v1/token"},  # a tampered configuration must not receive the identity token
        {"token_url": "http://sts.googleapis.com/v1/token"},
        {"service_account_impersonation_url": "https://evil.example.com/x:generateAccessToken"},
        {"service_account_impersonation_url": "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/a@b.iam.gserviceaccount.com:signBlob"},
        {"credential_source": {"url": "http://169.254.169.254/token"}},  # URL source
        {"credential_source": {"executable": {"command": "calc.exe"}}},  # executable source: would let a config file run programs
        {"credential_source": {"file": "x.txt", "url": "http://x"}},
        {"audience": "//iam.googleapis.com/not-a-pool"},
        {"type": "service_account"},
        {"subject_token_type": ""},
        {"credential_source": {"file": "x.txt", "format": {"type": "json"}}},  # json without a field name
    ],
)
def test_unsafe_or_unsupported_configurations_are_refused_before_anything_is_sent(tmp_path, overrides):
    path, _ = write_config(tmp_path, **overrides)
    with pytest.raises(DriveAuthPermanentError):
        ExternalAccountConfig.from_file(path)


def test_a_missing_or_malformed_configuration_file_is_a_permanent_secret_free_error(tmp_path):
    with pytest.raises(DriveAuthPermanentError) as missing:
        ExternalAccountConfig.from_file(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(DriveAuthPermanentError):
        ExternalAccountConfig.from_file(bad)
    assert str(tmp_path) not in str(missing.value)


def test_a_missing_empty_or_oversized_subject_token_file_is_a_permanent_error_that_names_no_path(tmp_path):
    path, token_file = write_config(tmp_path)
    cfg = ExternalAccountConfig.from_file(path)
    token_file.write_text("   \n", encoding="utf-8")
    with pytest.raises(DriveAuthPermanentError) as empty:
        cfg.read_subject_token()
    token_file.write_text("x" * (70 * 1024), encoding="utf-8")
    with pytest.raises(DriveAuthPermanentError):
        cfg.read_subject_token()
    token_file.unlink()
    with pytest.raises(DriveAuthPermanentError) as gone:
        cfg.read_subject_token()
    assert str(tmp_path) not in str(gone.value) and str(tmp_path) not in str(empty.value)


@pytest.mark.parametrize("status, expected", [(400, DriveAuthPermanentError), (401, DriveAuthPermanentError), (403, DriveAuthPermanentError), (429, DriveAuthTransientError), (503, DriveAuthTransientError)])
def test_sts_failures_are_classified_and_never_echo_the_response_body(tmp_path, status, expected):
    path, _ = write_config(tmp_path)
    rec = Recorder(sts_status=status, sts_body={"error": "invalid_grant", "error_description": f"secret detail {SUBJECT}"})
    with pytest.raises(expected) as caught:
        provider(path, rec).get_token()
    text = str(caught.value)
    assert SUBJECT not in text and "secret detail" not in text


def test_iam_failures_use_the_status_code_not_the_message(tmp_path):
    path, _ = write_config(tmp_path)
    rec = Recorder(iam_status=403, iam_body={"error": {"code": 403, "status": "PERMISSION_DENIED", "message": f"caller {FEDERATED} may not impersonate x@y.iam.gserviceaccount.com"}})
    with pytest.raises(DriveAuthPermanentError) as caught:
        provider(path, rec).get_token()
    assert "PERMISSION_DENIED" in str(caught.value)
    assert FEDERATED not in str(caught.value) and "iam.gserviceaccount.com" not in str(caught.value)


def test_network_errors_are_transient_and_carry_no_request_data(tmp_path):
    path, _ = write_config(tmp_path)

    def boom(request):
        raise httpx.ConnectError(f"cannot connect with {SUBJECT}")

    p = ExternalAccountTokenProvider(ExternalAccountConfig.from_file(path), "scope", httpx.Client(transport=httpx.MockTransport(boom)), settings=TokenSettings())
    with pytest.raises(DriveAuthTransientError) as caught:
        p.get_token()
    assert SUBJECT not in str(caught.value)


def test_malformed_answers_are_transient_not_crashes(tmp_path):
    path, _ = write_config(tmp_path)
    for body in ({}, {"access_token": ""}, {"access_token": 5}, []):
        with pytest.raises(DriveAuthTransientError):
            provider(path, Recorder(sts_body=body)).get_token()


def test_reprs_hide_everything(tmp_path):
    path, _ = write_config(tmp_path)
    p = provider(path, Recorder())
    assert "hidden" in repr(p) and "hidden" in repr(p._config)
    assert SUBJECT not in repr(p) and AUDIENCE not in repr(p._config)


# ------------------------------------------------------------------------------------------------------------------------------ configuration & selection


def env_for(tmp_path, mode="external_account", **extra):
    path, _ = write_config(tmp_path)
    base = {
        "SPINCO_GDRIVE_ROOT_FOLDER_ID": "1AbCdEfGhIjKlMnOpQrSt",
        "SPINCO_GDRIVE_AUTH_MODE": mode,
        "SPINCO_GDRIVE_WIF_CONFIG_FILE": str(path),
        "SPINCO_ENVIRONMENT": "production",
        "SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION": "0AProdSharedDriveId1",
    }
    base.update(extra)
    return base


def test_external_account_is_a_recognised_mode_and_needs_its_file(tmp_path):
    cfg = drive_config.load_runtime_config(env_for(tmp_path))
    assert cfg.auth_mode == "external_account" and cfg.wif_config_file is not None and cfg.credentials_file is None
    with pytest.raises(DriveConfigError):
        drive_config.load_runtime_config(env_for(tmp_path, SPINCO_GDRIVE_WIF_CONFIG_FILE=""))
    with pytest.raises(DriveConfigError):
        drive_config.load_runtime_config(env_for(tmp_path, SPINCO_GDRIVE_WIF_CONFIG_FILE=str(tmp_path / "missing.json")))
    with pytest.raises(DriveConfigError):
        drive_config.load_runtime_config(env_for(tmp_path, SPINCO_GDRIVE_AUTH_MODE="nonsense"))


def test_the_runtime_builds_the_federated_provider_for_that_mode(tmp_path):
    cfg = drive_config.load_runtime_config(env_for(tmp_path))
    built = build_token_provider(cfg, httpx.Client(transport=httpx.MockTransport(Recorder())))
    assert isinstance(built, ExternalAccountTokenProvider)


def test_the_staging_user_oauth_authorisation_is_refused_in_production(tmp_path):
    client = tmp_path / "client.json"
    client.write_text("{}", encoding="utf-8")
    env = env_for(tmp_path, mode="oauth_user", SPINCO_GDRIVE_OAUTH_CLIENT_FILE=str(client), SPINCO_GDRIVE_OAUTH_TOKEN_FILE=str(tmp_path / "token.json"))
    with pytest.raises(DriveConfigError, match="not allowed in the production"):
        drive_config.load_runtime_config(env)
    # ... while staging keeps working exactly as before
    staging = dict(env, SPINCO_ENVIRONMENT="staging", SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING="0AStagingDriveId1")
    staging.pop("SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION")
    assert drive_config.load_runtime_config(staging).auth_mode == "oauth_user"
