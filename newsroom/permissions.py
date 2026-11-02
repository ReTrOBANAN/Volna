"""Deny-by-default authorization. A profile and its single role Group must agree."""
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q

from .models import Department, EmployeeProfile, Event, Role, UploadedFile


ROLE_CAPABILITIES = {
    Role.DIRECTOR: {'shifts_view', 'shifts_manage', 'bookings_view', 'bookings_manage', 'bookings_create', 'events_view', 'events_manage', 'events_create', 'files_view', 'files_upload', 'employees_manage', 'proposals_view', 'proposals_create', 'news_view'},
    Role.RADIO_CHIEF: {'shifts_view', 'shifts_manage', 'bookings_view', 'bookings_manage', 'bookings_create', 'events_view', 'files_view', 'files_upload', 'employees_manage', 'proposals_view', 'news_view'},
    Role.RADIO_REPORTER: {'shifts_view', 'bookings_view', 'bookings_create', 'files_view', 'files_upload', 'news_view'},
    Role.ENGINEER: {'bookings_view', 'files_view', 'files_upload'},
    Role.TV_CHIEF: {'bookings_view', 'bookings_manage', 'bookings_create', 'events_view', 'events_manage', 'events_create', 'files_view', 'files_upload', 'employees_manage', 'proposals_view'},
    Role.TV_REPORTER: {'bookings_view', 'bookings_create', 'events_view', 'events_manage', 'events_create', 'files_view', 'files_upload'},
    Role.OPERATOR: {'events_view', 'files_view', 'files_upload'},
    Role.PHOTOGRAPHER: {'events_view', 'files_view', 'files_upload'},
    Role.EDITOR: {'events_view', 'files_view', 'files_upload'},
    Role.SITE_CHIEF: {'events_view', 'bookings_view', 'files_view', 'files_upload', 'employees_manage'},
    Role.SITE_EDITOR: {'events_view', 'bookings_view', 'files_view', 'files_upload'},
    Role.PRODUCER: {'events_view', 'bookings_view', 'files_view', 'files_upload', 'proposals_view', 'proposals_create'},
}
CAPABILITIES = set().union(*ROLE_CAPABILITIES.values())
ROLE_DEPARTMENTS = {
    'director': 'management', 'radio_chief': 'radio', 'radio_reporter': 'radio',
    'engineer': 'tv', 'tv_chief': 'tv', 'tv_reporter': 'tv', 'operator': 'tv',
    'photographer': 'tv', 'editor': 'tv', 'site_chief': 'site', 'site_editor': 'site',
    'producer': 'production',
}


def setup_roles():
    for code, _ in Role.choices:
        Group.objects.get_or_create(name=code)
    for slug, name in [('management', 'Дирекция'), ('radio', 'Радио'), ('tv', 'Телевидение'), ('site', 'Новостной сайт'), ('production', 'Продюсерский отдел')]:
        Department.objects.get_or_create(slug=slug, defaults={'name': name})


@transaction.atomic
def assign_role(user, role, department):
    """Trusted setup helper; web requests must use services.save_employee."""
    if role not in Role.values or department.slug != ROLE_DEPARTMENTS[role]:
        raise PermissionDenied('Роль не соответствует подразделению.')
    setup_roles()
    user.groups.set([Group.objects.get(name=role)])
    profile, _ = EmployeeProfile.objects.update_or_create(user=user, defaults={'role': role, 'department': department})
    user._state.fields_cache.pop('employee', None)
    return profile


def role_of(user):
    if not user.is_authenticated or not user.is_active:
        return None
    try:
        profile_role = user.employee.role
    except EmployeeProfile.DoesNotExist:
        return None
    roles = list(user.groups.filter(name__in=Role.values).values_list('name', flat=True))
    return profile_role if roles == [profile_role] else None


def can(user, capability):
    return capability in ROLE_CAPABILITIES.get(role_of(user), set())


def require_capability(user, capability):
    if not can(user, capability):
        raise PermissionDenied('Ваша роль не разрешает это действие.')


def visible_events(user):
    return Event.objects.all() if can(user, 'events_view') else Event.objects.none()


def can_edit_booking(user, booking):
    if can(user, 'bookings_manage'):
        return True
    return can(user, 'bookings_create') and booking.created_by_id == user.pk and booking.status in {'planned', 'recording'}


def can_edit_event(user, event):
    return can(user, 'events_manage') and (event.status not in {'approved', 'published'} or role_of(user) in {Role.DIRECTOR, Role.TV_CHIEF})


def can_view_event_materials(user, event):
    role = role_of(user)
    if not role:
        return False
    if role in {Role.DIRECTOR, Role.TV_CHIEF}:
        return True
    if role in {Role.SITE_CHIEF, Role.SITE_EDITOR}:
        return event.status in {'approved', 'published'}
    return user.pk in {event.reporter_id, event.camera_id, event.photographer_id, event.editor_id, event.created_by_id}


def visible_files(user):
    if not can(user, 'files_view'):
        return UploadedFile.objects.none()
    role = role_of(user)
    files = UploadedFile.objects.select_related('uploaded_by', 'event')
    if role in {Role.DIRECTOR, Role.TV_CHIEF}:
        return files
    if role in {Role.SITE_CHIEF, Role.SITE_EDITOR}:
        return files.filter(Q(event__isnull=True) | Q(event__status__in=['approved', 'published']))
    return files.filter(Q(event__isnull=True) | Q(event__reporter=user) | Q(event__camera=user) | Q(event__photographer=user) | Q(event__editor=user) | Q(event__created_by=user)).distinct()


def can_view_file(user, uploaded_file):
    return can(user, 'files_view') and (uploaded_file.event_id is None or can_view_event_materials(user, uploaded_file.event))


def allowed_employee_roles(user):
    return {
        Role.DIRECTOR: list(Role.values), Role.RADIO_CHIEF: [Role.RADIO_REPORTER],
        Role.TV_CHIEF: [Role.TV_REPORTER, Role.OPERATOR, Role.PHOTOGRAPHER, Role.EDITOR, Role.ENGINEER],
        Role.SITE_CHIEF: [Role.SITE_EDITOR],
    }.get(role_of(user), [])


def can_manage_employee(user, target):
    if role_of(user) == Role.DIRECTOR:
        return True
    try:
        return can(user, 'employees_manage') and target.employee.department_id == user.employee.department_id and target.employee.role in allowed_employee_roles(user)
    except EmployeeProfile.DoesNotExist:
        return False


def users_with_roles(*roles):
    return User.objects.filter(is_active=True, employee__role__in=roles, groups__name__in=roles).distinct()
