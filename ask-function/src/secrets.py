"""Read the Anthropic key (and the Turnstile secret) from SSM at cold start.

The key never lives in an environment variable — that is the one thing solidago's
ask-lambda module got wrong and this module fixes. It sits in an SSM SecureString,
KMS-encrypted, and is read once per container when the function cold-starts, then
held in memory for the life of the container. The execution role can read exactly
the two parameter paths it is given, and nothing else.
"""

from __future__ import annotations

from functools import lru_cache


class SecretUnavailable(RuntimeError):
    """The SecureString could not be read: it does not exist yet, or this role
    may not read it. The handler turns this into a maintenance-style reply
    instead of a 500, so an unconfigured box still answers politely."""


@lru_cache(maxsize=8)
def read_secure_string(path: str, region: str) -> str:
    """Fetch and decrypt one SSM SecureString. Cached for the container's life.

    boto3 is part of the Lambda runtime, so it is imported here rather than
    listed as a dependency. The result is memoised so warm invocations never
    call SSM again.
    """
    import boto3

    from botocore.exceptions import ClientError

    client = boto3.client("ssm", region_name=region)
    try:
        resp = client.get_parameter(Name=path, WithDecryption=True)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        raise SecretUnavailable(f"{code or 'error'} reading SSM parameter {path}") from exc
    return resp["Parameter"]["Value"]
