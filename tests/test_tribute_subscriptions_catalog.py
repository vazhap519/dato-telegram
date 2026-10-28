"""Offline contract tests for read-only Tribute subscription catalogue parsing."""
import unittest

from scripts.check_tribute_subscriptions import parse_catalog


class TributeSubscriptionCatalogueTests(unittest.TestCase):
    def test_documented_catalog_schema(self):
        data = {"result": [{
            "subscriptionId": 1234,
            "name": "Test Practice",
            "currency": "USD",
            "periods": [
                {"periodId": 91, "period": "monthly", "price": 1},
                {"periodId": 92, "period": "yearly", "price": 10},
            ],
        }]}
        rows = parse_catalog(data)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["subscription_id"], 1234)
        self.assertEqual(rows[0]["period_id"], 91)
        self.assertEqual(rows[0]["price"], 1)
        self.assertEqual(rows[0]["cadence"], "monthly")

    def test_missing_envelope_rejected(self):
        for obj in ({}, [], {"result": {}}, None):
            with self.subTest(obj=obj), self.assertRaises(ValueError):
                parse_catalog(obj)

    def test_invalid_rows_skipped(self):
        data = {"result": [
            None, {"subscriptionId": "wrong", "periods": []},
            {"subscriptionId": 1, "periods": [
                {"periodId": True, "period": "monthly", "price": 1},
                {"periodId": 4, "period": "monthly", "price": "1"},
                {"periodId": 5, "period": "monthly", "price": 1},
            ]},
        ]}
        rows = parse_catalog(data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["period_id"], 5)


if __name__ == "__main__":
    unittest.main()
