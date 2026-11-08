from celery import shared_task


@shared_task(name="payments.reconcile_gateway")
def reconcile_gateway() -> dict:
    """Daily: settlement reports, missed-payment recovery and checks. Does nothing while Razorpay is not usable."""
    from iic_booking.payments import gateway_mode

    problem = gateway_mode.configuration_problem()
    if problem:
        return {"skipped": problem}
    from iic_booking.payments.reconciliation import reconcile

    return reconcile()
