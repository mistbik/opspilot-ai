# Rate limits

An HTTP 429 indicates the receiver is rate limiting requests. Check the receiver's quota and any Retry-After header at the receiver. Reduce the request rate or wait for the throttling window to clear.

The relay uses jittered exponential backoff and currently does not interpret Retry-After. If automatic attempts are exhausted, approve a manual retry only after confirming that the receiver can accept another request.
