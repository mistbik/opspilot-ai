# Authentication and signing failures

An HTTP 401 indicates the receiver declined authentication. A mismatched, expired, or rotated signing secret is one possible cause. Other causes include proxy rules and signature format or body canonicalization differences.

Compare the receiver's active signing configuration with the configured endpoint secret without exposing either secret. Verify the HMAC-SHA256 signature against the original raw request bytes and check the expected signature header. Correct the configuration before replaying.
