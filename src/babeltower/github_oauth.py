from urllib.parse import urlencode

import httpx

from babeltower.config import get_settings

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"


def build_oauth_url(state: str) -> str:
    settings = get_settings()
    query = urlencode(
        {
            "client_id": settings.github_oauth_client_id,
            "redirect_uri": f"{settings.server_base_url}/v1/register/oauth/callback",
            "scope": "read:user",
            "state": state,
        }
    )
    return f"{GITHUB_AUTHORIZE_URL}?{query}"


async def exchange_code(code: str) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            GITHUB_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.github_oauth_client_id,
                "client_secret": settings.github_oauth_client_secret,
                "code": code,
            },
        )
        response.raise_for_status()
        return response.json()


async def get_user_id(access_token: str) -> int:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            GITHUB_USER_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
            },
        )
        response.raise_for_status()
        return int(response.json()["id"])
