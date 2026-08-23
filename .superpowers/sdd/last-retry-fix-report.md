# Compression WeCom rejection retry fix

- Changed `_CompressionWeComRejectedError` handling in `momentum_compression_alerts.py` to use the existing retry/backoff policy.
- Preserved `ReadTimeout` and `ConnectionError` as terminal indeterminate outcomes with no resend.
- Regression coverage now verifies rejection retries at `RETRY_DELAYS_MS`, stops after `MAX_ATTEMPTS` (five), sanitizes the webhook, and remains terminal failed only at the limit.
- Verification: focused compression/reflow tests `49 passed`; full unittest suite `572 passed`; `py_compile` and `git diff --check` passed.
