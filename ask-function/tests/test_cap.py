"""The durable daily cap holds across containers and fails closed on the ceiling."""

import unittest

import support  # noqa: F401 — sets up sys.path
from cap import DailyCap


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeDynamo:
    """A one-table DynamoDB stand-in enforcing the conditional increment."""

    def __init__(self):
        self.store = {}
        self.last_ttl = None

    def update_item(self, **kw):
        day = kw["Key"]["day"]["S"]
        cap = int(kw["ExpressionAttributeValues"][":cap"]["N"])
        self.last_ttl = int(kw["ExpressionAttributeValues"][":ttl"]["N"])
        present = day in self.store
        current = self.store.get(day, 0)
        # attribute_not_exists(#c) OR #c < :cap
        if present and current >= cap:
            raise FakeClientError("ConditionalCheckFailedException")
        self.store[day] = current + 1
        return {"Attributes": {"count": {"N": str(self.store[day])}}}


class CapTest(unittest.TestCase):
    def test_increments_until_the_cap_then_refuses(self):
        cap = DailyCap("t", "us-east-1", client=FakeDynamo())
        results = [cap.reserve("2026-10-03", 3, now_epoch=1000) for _ in range(4)]
        self.assertEqual([(a, c) for a, c in results],
                         [(True, 1), (True, 2), (True, 3), (False, 3)])

    def test_each_day_has_its_own_counter(self):
        cap = DailyCap("t", "us-east-1", client=FakeDynamo())
        self.assertEqual(cap.reserve("2026-10-03", 1, now_epoch=1000), (True, 1))
        self.assertEqual(cap.reserve("2026-10-03", 1, now_epoch=1000), (False, 1))
        # A new UTC day starts fresh.
        self.assertEqual(cap.reserve("2026-10-04", 1, now_epoch=1000), (True, 1))

    def test_ttl_is_stamped_for_free_cleanup(self):
        fake = FakeDynamo()
        cap = DailyCap("t", "us-east-1", client=fake)
        cap.reserve("2026-10-03", 5, now_epoch=1000, ttl_seconds=100)
        self.assertEqual(fake.last_ttl, 1100)

    def test_a_non_cap_error_is_not_swallowed(self):
        class Boom:
            def update_item(self, **kw):
                raise FakeClientError("ProvisionedThroughputExceededException")

        cap = DailyCap("t", "us-east-1", client=Boom())
        with self.assertRaises(FakeClientError):
            cap.reserve("2026-10-03", 3, now_epoch=1000)


if __name__ == "__main__":
    unittest.main()
