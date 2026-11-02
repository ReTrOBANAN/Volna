from django.utils import timezone
from .models import EmployeeProfile, Notification
from .permissions import CAPABILITIES, ROLE_CAPABILITIES, role_of


def workspace(request):
    if not request.user.is_authenticated:
        return {}
    try:
        employee = request.user.employee
    except EmployeeProfile.DoesNotExist:
        employee = None
    capabilities = ROLE_CAPABILITIES.get(role_of(request.user), set())
    return {
        'employee': employee,
        'access': {key: key in capabilities for key in CAPABILITIES},
        'unread_count': Notification.objects.filter(recipient=request.user, is_read=False, available_at__lte=timezone.now()).count(),
    }
