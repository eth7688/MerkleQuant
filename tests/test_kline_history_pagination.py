import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import screener


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http_{self.status_code}")

    def json(self):
        return self._payload


def binance_row(open_time):
    return [
        open_time, "100", "101", "99", "100", "1",
        open_time + 59_999, "1", 1, "1", "1", "0",
    ]


def bitget_row(open_time):
    return [str(open_time), "100", "101", "99", "100", "1"]


class KlineHistoryPaginationTest(unittest.TestCase):
    def history_fetcher(self):
        fetcher = getattr(screener, "fetch_klines_range", None)
        self.assertIsNotNone(fetcher, "range paginator must exist")
        return fetcher

    def test_binance_paginates_forward_until_full_range_is_covered(self):
        step_ms = 60_000
        first_ms = 1_699_999_980_000
        candles = [
            binance_row(first_ms + index * step_ms)
            for index in range(1_205)
        ]
        calls = []

        def fake_get(url, params, timeout):
            calls.append((url, dict(params)))
            start_ms = int(params["startTime"])
            end_ms = int(params["endTime"])
            limit = int(params["limit"])
            page = [
                row for row in candles
                if start_ms <= int(row[0]) <= end_ms
            ][:limit]
            return FakeResponse(page)

        with patch("screener.requests.get", side_effect=fake_get):
            frame = self.history_fetcher()(
                "BTCUSDT",
                "1m",
                first_ms,
                int(candles[-1][0]),
                exchange="binance",
                market_type="futures",
                price_type="mark",
                pause_seconds=0,
            )

        self.assertIsNotNone(frame)
        self.assertTrue(frame.attrs.get("range_complete"))
        self.assertEqual(len(frame), len(candles))
        self.assertEqual(frame["ot"].tolist(), [row[0] for row in candles])
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all("/fapi/v1/markPriceKlines" in call[0] for call in calls))
        starts = [int(call[1]["startTime"]) for call in calls]
        self.assertEqual(starts[0], first_ms)
        self.assertTrue(all(left < right for left, right in zip(starts, starts[1:])))

    def test_bitget_paginates_backward_with_exclusive_end_time(self):
        step_ms = 60_000
        first_ms = 1_699_999_980_000
        candles = [
            bitget_row(first_ms + index * step_ms)
            for index in range(250)
        ]
        calls = []

        def fake_get(url, params, timeout):
            calls.append((url, dict(params)))
            end_ms = int(params["endTime"])
            limit = int(params["limit"])
            eligible = [row for row in candles if int(row[0]) < end_ms]
            page = list(reversed(eligible[-limit:]))
            return FakeResponse({"code": "00000", "data": page})

        with patch("screener.requests.get", side_effect=fake_get):
            frame = self.history_fetcher()(
                "BTCUSDT",
                "1m",
                first_ms,
                int(candles[-1][0]),
                exchange="bitget",
                market_type="futures",
                pause_seconds=0,
            )

        self.assertIsNotNone(frame)
        self.assertTrue(frame.attrs.get("range_complete"))
        self.assertEqual(len(frame), len(candles))
        self.assertEqual(frame["ot"].tolist(), [int(row[0]) for row in candles])
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all("/history-candles" in call[0] for call in calls))
        ends = [int(call[1]["endTime"]) for call in calls]
        self.assertEqual(ends[0], int(candles[-1][0]) + step_ms)
        self.assertTrue(all(left > right for left, right in zip(ends, ends[1:])))

    def test_bitget_retries_transient_page_failures_before_failing_closed(self):
        step_ms = 60_000
        first_ms = 1_699_999_980_000
        candles = [bitget_row(first_ms + index * step_ms) for index in range(10)]

        for transient in (
            FakeResponse({"code": "42900", "data": []}, status_code=429),
            FakeResponse({"code": "00000", "data": []}),
        ):
            calls = []

            def fake_get(url, params, timeout):
                calls.append(dict(params))
                if len(calls) == 1:
                    return transient
                return FakeResponse({"code": "00000", "data": list(reversed(candles))})

            with self.subTest(status=transient.status_code), patch(
                "screener.requests.get", side_effect=fake_get
            ), patch("screener.time.sleep") as sleep:
                frame = self.history_fetcher()(
                    "BTCUSDT",
                    "1m",
                    first_ms,
                    int(candles[-1][0]),
                    exchange="bitget",
                    market_type="futures",
                    pause_seconds=0,
                )

            self.assertIsNotNone(frame)
            self.assertEqual(frame["ot"].tolist(), [int(row[0]) for row in candles])
            self.assertEqual(len(calls), 2)
            sleep.assert_called_once()

    def test_bitget_stalled_page_fails_closed_instead_of_returning_partial_data(self):
        step_ms = 60_000
        first_ms = 1_699_999_980_000
        repeated_page = [
            bitget_row(first_ms + index * step_ms)
            for index in range(300, 500)
        ]
        calls = []

        def fake_get(url, params, timeout):
            calls.append(dict(params))
            return FakeResponse({
                "code": "00000",
                "data": list(reversed(repeated_page)),
            })

        with patch("screener.requests.get", side_effect=fake_get):
            frame = self.history_fetcher()(
                "BTCUSDT",
                "1m",
                first_ms,
                first_ms + 499 * step_ms,
                exchange="bitget",
                market_type="futures",
                pause_seconds=0,
            )

        self.assertIsNone(frame)
        self.assertEqual(len(calls), 2)

    def test_malformed_exchange_row_fails_closed(self):
        first_ms = 1_700_000_000_000

        def fake_get(url, params, timeout):
            return FakeResponse([
                binance_row(first_ms),
                [first_ms + 60_000, "100"],
            ])

        with patch("screener.requests.get", side_effect=fake_get):
            frame = self.history_fetcher()(
                "BTCUSDT",
                "1m",
                first_ms,
                first_ms + 60_000,
                exchange="binance",
                market_type="futures",
                pause_seconds=0,
            )

        self.assertIsNone(frame)

    def test_explicit_future_end_is_clamped_to_last_closed_candle(self):
        step_ms = 60_000
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        calls = []

        def fake_get(url, params, timeout):
            calls.append(dict(params))
            return FakeResponse([])

        with patch("screener.requests.get", side_effect=fake_get):
            frame = self.history_fetcher()(
                "BTCUSDT",
                "1m",
                now_ms - 10 * step_ms,
                now_ms + 10 * step_ms,
                exchange="binance",
                market_type="futures",
                pause_seconds=0,
            )

        self.assertIsNone(frame)
        self.assertEqual(len(calls), 1)
        after_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        last_closed_open = (after_ms // step_ms) * step_ms - step_ms
        self.assertLessEqual(int(calls[0]["endTime"]), last_closed_open)

    def test_binance_short_page_before_end_fails_closed(self):
        step_ms = 60_000
        first_ms = 1_699_999_980_000
        calls = []

        def fake_get(url, params, timeout):
            calls.append(dict(params))
            if len(calls) == 1:
                return FakeResponse([binance_row(first_ms)])
            return FakeResponse([])

        with patch("screener.requests.get", side_effect=fake_get):
            frame = self.history_fetcher()(
                "BTCUSDT",
                "1m",
                first_ms,
                first_ms + 10 * step_ms,
                exchange="binance",
                market_type="futures",
                pause_seconds=0,
            )

        self.assertIsNone(frame)

    def test_binance_empty_page_before_end_fails_closed(self):
        step_ms = 60_000
        first_ms = 1_699_999_980_000
        with patch(
            "screener.requests.get",
            return_value=FakeResponse([]),
        ):
            frame = self.history_fetcher()(
                "BTCUSDT",
                "1m",
                first_ms,
                first_ms + 10 * step_ms,
                exchange="binance",
                market_type="futures",
                pause_seconds=0,
            )

        self.assertIsNone(frame)

    def test_bitget_short_page_before_start_fails_closed(self):
        step_ms = 60_000
        first_ms = 1_699_999_980_000
        end_ms = first_ms + 10 * step_ms
        calls = []

        def fake_get(url, params, timeout):
            calls.append(dict(params))
            if len(calls) == 1:
                return FakeResponse({
                    "code": "00000",
                    "data": [bitget_row(end_ms)],
                })
            return FakeResponse({"code": "00000", "data": []})

        with patch("screener.requests.get", side_effect=fake_get):
            frame = self.history_fetcher()(
                "BTCUSDT",
                "1m",
                first_ms,
                end_ms,
                exchange="bitget",
                market_type="futures",
                pause_seconds=0,
            )

        self.assertIsNone(frame)

    def test_bitget_empty_page_before_start_fails_closed(self):
        step_ms = 60_000
        first_ms = 1_699_999_980_000
        with patch(
            "screener.requests.get",
            return_value=FakeResponse({"code": "00000", "data": []}),
        ):
            frame = self.history_fetcher()(
                "BTCUSDT",
                "1m",
                first_ms,
                first_ms + 10 * step_ms,
                exchange="bitget",
                market_type="futures",
                pause_seconds=0,
            )

        self.assertIsNone(frame)

    def test_binance_internal_timestamp_gap_fails_closed(self):
        step_ms = 60_000
        first_ms = 1_699_999_980_000
        with patch(
            "screener.requests.get",
            return_value=FakeResponse([
                binance_row(first_ms),
                binance_row(first_ms + 2 * step_ms),
            ]),
        ):
            frame = self.history_fetcher()(
                "BTCUSDT",
                "1m",
                first_ms,
                first_ms + 2 * step_ms,
                exchange="binance",
                market_type="futures",
                pause_seconds=0,
            )

        self.assertIsNone(frame)

    def test_bitget_internal_timestamp_gap_fails_closed(self):
        step_ms = 60_000
        first_ms = 1_699_999_980_000
        with patch(
            "screener.requests.get",
            return_value=FakeResponse({
                "code": "00000",
                "data": [
                    bitget_row(first_ms + 2 * step_ms),
                    bitget_row(first_ms),
                ],
            }),
        ):
            frame = self.history_fetcher()(
                "BTCUSDT",
                "1m",
                first_ms,
                first_ms + 2 * step_ms,
                exchange="bitget",
                market_type="futures",
                pause_seconds=0,
            )

        self.assertIsNone(frame)

    def test_binance_interval_shifted_timestamps_fail_closed(self):
        step_ms = 60_000
        first_ms = 1_699_999_980_000
        with patch(
            "screener.requests.get",
            return_value=FakeResponse([
                binance_row(first_ms + 1),
                binance_row(first_ms + step_ms + 1),
            ]),
        ):
            frame = self.history_fetcher()(
                "BTCUSDT",
                "1m",
                first_ms,
                first_ms + 2 * step_ms,
                exchange="binance",
                market_type="futures",
                pause_seconds=0,
            )

        self.assertIsNone(frame)

    def test_bitget_interval_shifted_timestamps_fail_closed(self):
        step_ms = 60_000
        first_ms = 1_699_999_980_000
        with patch(
            "screener.requests.get",
            return_value=FakeResponse({
                "code": "00000",
                "data": [
                    bitget_row(first_ms + step_ms + 1),
                    bitget_row(first_ms + 1),
                ],
            }),
        ):
            frame = self.history_fetcher()(
                "BTCUSDT",
                "1m",
                first_ms,
                first_ms + 2 * step_ms,
                exchange="bitget",
                market_type="futures",
                pause_seconds=0,
            )

        self.assertIsNone(frame)


if __name__ == "__main__":
    unittest.main()
