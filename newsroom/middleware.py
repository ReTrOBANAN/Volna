from .permissions import can
from .services import sync_booking_statuses


class BookingStatusSyncMiddleware:
    """Refresh derived booking states before authorized requests validate or render data."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and can(request.user, 'bookings_view'):
            sync_booking_statuses()
        return self.get_response(request)
