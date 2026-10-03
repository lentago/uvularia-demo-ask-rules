"""A durable daily cap in DynamoDB — one counter row per UTC day.

solidago's ask-lambda kept its cap in per-container memory, which means the real
ceiling is `containers * cap` and a burst of cold starts blows straight past it.
This counts in DynamoDB with a single atomic conditional increment, so the cap
holds no matter how many containers are warm.

The table has one item per UTC day (`day` = "YYYY-MM-DD"). Each answerable
request does one `UpdateItem` that increments `count` only while it is below the
cap; the condition failing *is* the cap being hit. A short TTL on each row lets
DynamoDB delete yesterday's counters for free, so the table never grows and stays
inside the always-free tier.
"""

from __future__ import annotations

TWO_DAYS = 2 * 24 * 60 * 60


def _is_conditional_failure(exc: Exception) -> bool:
    """True if a DynamoDB error is the condition check failing (cap reached).

    We read the botocore error shape rather than importing botocore's exception
    type, so the unit tests can raise a stand-in with the same ``response`` shape
    without boto3 installed.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class DailyCap:
    """Atomic per-UTC-day counter backed by one DynamoDB table."""

    def __init__(self, table_name: str, region: str, client=None):
        self.table_name = table_name
        self.region = region
        self._injected = client

    def _client(self):
        if self._injected is not None:
            return self._injected
        import boto3

        self._injected = boto3.client("dynamodb", region_name=self.region)
        return self._injected

    def reserve(self, day: str, cap: int, now_epoch: int, ttl_seconds: int = TWO_DAYS):
        """Try to claim one answer for ``day``. Returns ``(allowed, count)``.

        ``count`` is the number of answers used today *after* this one when
        allowed (1..cap), or the cap itself when the day is already full. The
        increment and the ceiling check are one operation, so two concurrent
        containers can never both slip past the last slot.
        """
        client = self._client()
        try:
            resp = client.update_item(
                TableName=self.table_name,
                Key={"day": {"S": day}},
                UpdateExpression=(
                    "SET #c = if_not_exists(#c, :zero) + :one, "
                    "#e = if_not_exists(#e, :ttl)"
                ),
                ConditionExpression="attribute_not_exists(#c) OR #c < :cap",
                ExpressionAttributeNames={"#c": "count", "#e": "expires"},
                ExpressionAttributeValues={
                    ":zero": {"N": "0"},
                    ":one": {"N": "1"},
                    ":cap": {"N": str(cap)},
                    ":ttl": {"N": str(now_epoch + ttl_seconds)},
                },
                ReturnValues="UPDATED_NEW",
            )
            count = int(resp["Attributes"]["count"]["N"])
            return True, count
        except Exception as exc:  # noqa: BLE001 — re-raised unless it is the cap
            if _is_conditional_failure(exc):
                return False, cap
            raise
