"""Read the Anthropic key (and the Turnstile secret) from SSM at cold start.

The key never lives in an environment variable — that is the one thing solidago's
ask-lambda module got wrong and this module fixes. It sits in an SSM SecureString,
KMS-encrypted, and is read once per container when the function cold-starts, then
held in memory for the life of the container. The execution role can read exactly
the two parameter paths it is given, and nothing else.
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=8)
def read_secure_string(path: str, region: str) -> str:
    """Fetch and decrypt one SSM SecureString. Cached for the container's life.

    boto3 is part of the Lambda runtime, so it is imported here rather than
    listed as a dependency. The result is memoised so warm invocations never
    call SSM again.
    """
    import boto3

    client = boto3.client("ssm", region_name=region)
    resp = client.get_parameter(Name=path, WithDecryption=True)
    return resp["Parameter"]["Value"]
