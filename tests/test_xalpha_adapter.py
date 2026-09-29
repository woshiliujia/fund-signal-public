import os
import unittest
from unittest.mock import patch

import fund_core
import screenshot_import
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


class RegistrationOptionTests(unittest.TestCase):
    def test_public_registration_is_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(fund_core.public_registration_enabled())
        with patch.dict(os.environ, {"FS_ALLOW_PUBLIC_REGISTER": "true"}, clear=True):
            self.assertTrue(fund_core.public_registration_enabled())


class ScreenshotImportParsingTests(unittest.TestCase):
    def test_extracts_reviewable_fund_fields(self):
        fields = screenshot_import.parse_fund_details(
            "基金代码 000001\n持有金额 12,345.67元\n累计收益 +123.45元\n"
            "累计收益率 +1.23%\n持仓成本 1.2345"
        )
        self.assertEqual(fields["codes"], ["000001"])
        self.assertEqual(fields["hold_amount"], 12345.67)
        self.assertEqual(fields["reported_profit"], 123.45)
        self.assertEqual(fields["reported_profit_rate"], 1.23)
        self.assertEqual(fields["cost_price"], 1.2345)

    def test_accepts_amount_above_its_label(self):
        fields = screenshot_import.parse_fund_details(
            "000001\n12,345.67 元\n持有金额\n+1.23%\n累计收益率"
        )
        self.assertEqual(fields["hold_amount"], 12345.67)
        self.assertEqual(fields["reported_profit_rate"], 1.23)

    def test_pairs_multi_column_asset_card_values_with_their_labels(self):
        lines = [
            {"text": "金额（元）", "left": 430, "right": 560, "top": 100, "bottom": 130},
            {"text": "1,951.30", "left": 400, "right": 600, "top": 150, "bottom": 200},
            {"text": "昨日收益（元）", "left": 40, "right": 220, "top": 250, "bottom": 280},
            {"text": "持有收益（元）", "left": 330, "right": 510, "top": 250, "bottom": 280},
            {"text": "持有收益率", "left": 650, "right": 790, "top": 250, "bottom": 280},
            {"text": "-83.12", "left": 75, "right": 190, "top": 300, "bottom": 330},
            {"text": "+119.05", "left": 355, "right": 485, "top": 300, "bottom": 330},
            {"text": "+6.50%", "left": 675, "right": 785, "top": 300, "bottom": 330},
        ]
        fields = screenshot_import.parse_fund_layout(lines)
        self.assertEqual(fields["hold_amount"], 1951.30)
        self.assertEqual(fields["reported_profit"], 119.05)
        self.assertEqual(fields["reported_profit_rate"], 6.50)


if __name__ == "__main__":
    unittest.main()
