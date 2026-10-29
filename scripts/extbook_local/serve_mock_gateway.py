"""Local acceptance only: run the development server with the mock Razorpay gateway installed in its process.

Order creation then works in the browser (``order_MOCK...`` ids, synthetic test keys that cannot reach Razorpay); the
payment itself is delivered with ``extbook-local.ps1 pay-checkout <checkout id>`` through the real webhook endpoint.
Refuses to start outside the acceptance settings (see mock_razorpay.assert_acceptance).

Usage: ``extbook-local.ps1 backend -MockGateway [-Clock ...] [-Port 8000]``
"""

import sys

from _bootstrap import setup

if __name__ == "__main__":
    setup()
    from django.core.management import call_command

    from mock_razorpay import MockGateway

    port = sys.argv[1] if len(sys.argv) > 1 else "8000"
    with MockGateway().active():
        print("Mock Razorpay gateway active in this server process (synthetic keys; no network).")
        call_command("runserver", f"127.0.0.1:{port}", use_reloader=False)
