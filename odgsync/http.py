import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class AuthError(RuntimeError):
    """An account has no usable token and needs interactive auth."""


def with_retries(session: requests.Session | None = None) -> requests.Session:
    """Mount a retrying adapter that backs off on throttling and 5xx (honours Retry-After)."""
    session = session or requests.Session()
    retry = Retry(
        total=6,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=None,  # retry POSTs too; uploads and batch calls are safe to resend
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def check(response: requests.Response) -> requests.Response:
    """raise_for_status, but include the response body so API errors are readable in logs."""
    if response.status_code >= 400:
        raise requests.HTTPError(
            f"{response.status_code} {response.request.method} {response.url}: {response.text[:500]}",
            response=response,
        )
    return response
