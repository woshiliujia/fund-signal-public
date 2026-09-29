import unittest

import xalpha_adapter


class NormaliseTransactionsTests(unittest.TestCase):
    def test_accepts_supported_date_forms_and_keeps_xalpha_trade_semantics(self):
        rows = xalpha_adapter.normalise_transactions([
            {"date": "2025-01-06", "fund": "000001", "trade": "1000"},
            {"date": "2025/02/06", "fund": "000001", "trade": -12.5},
        ])
        self.assertEqual(rows[0], {"date": "2025/01/06", "fund": "000001", "trade": 1000.0})
        self.assertEqual(rows[1]["trade"], -12.5)

    def test_rejects_invalid_code_and_zero_trade(self):
        with self.assertRaises(xalpha_adapter.XalphaIntegrationError):
            xalpha_adapter.normalise_transactions([
                {"date": "2025-01-06", "fund": "ABC", "trade": 1}
            ])
        with self.assertRaises(xalpha_adapter.XalphaIntegrationError):
            xalpha_adapter.normalise_transactions([
                {"date": "2025-01-06", "fund": "000001", "trade": 0}
            ])


if __name__ == "__main__":
    unittest.main()
