"""Transactional application actions. Views must never write workflow fields directly."""
from datetime import timedelta, timezone as datetime_timezone
from pathlib import Path

from django.conf import settings
from django.contrib.auth import password_validation
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import (
    AuditEntry, EmployeeProfile, Event, EventAssignment, EventStatusHistory, Notification,
    Proposal, Role, StudioBooking, UploadedFile,
)
from .permissions import (
    ROLE_CAPABILITIES, ROLE_DEPARTMENTS, allowed_employee_roles, assign_role, can, can_edit_booking, can_edit_event,
    can_manage_employee, can_view_event_materials, can_view_file, require_capability, role_of, users_with_roles,
)


def audit(user, obj, action):
    if user is None or user.pk is None:
        raise ValueError('Для действия сотрудника требуется автор аудита.')
    AuditEntry.objects.create(actor=user, object_type=obj._meta.model_name, object_id=obj.pk, action=action)


def notify(recipients, title, text='', url='', available_at=None):
    ids = {u.pk for u in recipients if u and u.is_active}
    Notification.objects.bulk_create([
        Notification(recipient_id=pk, title=title, text=text, url=url, available_at=available_at or timezone.now())
        for pk in ids
    ])


def notify_engineers(booking):
    notify(users_with_roles(Role.ENGINEER), 'Новая видеосъёмка в студии', booking.title, booking.get_absolute_url())


def _booking_now(now=None):
    value = timezone.now() if now is None else now
    if timezone.is_naive(value):
        raise ValueError('Время синхронизации должно содержать часовой пояс.')
    return value.astimezone(datetime_timezone.utc)


def _booking_status_at(booking, now):
    if booking.status in {'completed', 'cancelled'}:
        return booking.status
    if not booking.starts_at or not booking.ends_at:
        return 'planned'
    for field in ('starts_at', 'ends_at'):
        if timezone.is_naive(getattr(booking, field)):
            raise ValidationError({field: 'Дата и время должны содержать часовой пояс.'})
    if booking.ends_at.astimezone(datetime_timezone.utc) <= now:
        return 'completed'
    if booking.starts_at.astimezone(datetime_timezone.utc) <= now:
        return 'recording'
    return 'planned'


def _notify_booking_completed(booking, now):
    roles = [role for role in Role.values if 'bookings_view' in ROLE_CAPABILITIES[role]]
    # An adjacent recording may already be running, so this does not promise availability.
    notify(users_with_roles(*roles), f'{booking.studio}: запись завершена',
           f'Запись «{booking.title}» завершена.', booking.get_absolute_url(), available_at=now)


def _record_clock_transition(booking, previous, now):
    AuditEntry.objects.create(
        actor=None, object_type='studiobooking', object_id=booking.pk,
        action=f'Автоматически: статус {previous} → {booking.status}',
    )
    if booking.status == 'completed':
        _notify_booking_completed(booking, now)


@transaction.atomic
def sync_booking_statuses(now=None, booking_ids=None):
    """Apply clock-derived changes once; terminal manual/automatic states stay terminal."""
    now = _booking_now(now)
    bookings = StudioBooking.objects.filter(status__in=['planned', 'recording']).filter(
        Q(ends_at__lte=now) | Q(status='planned', starts_at__lte=now) | Q(status='recording', starts_at__gt=now)
    )
    if booking_ids is not None:
        bookings = bookings.filter(pk__in=booking_ids)
    count = 0
    # Snapshot before updates: SQLite must not modify the cursor it is iterating.
    for booking in list(bookings.select_related('studio').order_by('pk')):
        previous = booking.status
        desired = _booking_status_at(booking, now)
        if desired == previous:
            continue
        won = StudioBooking.objects.filter(
            pk=booking.pk, status=previous, starts_at=booking.starts_at, ends_at=booking.ends_at,
        ).update(status=desired, updated_at=now)
        if not won:
            continue
        booking.status = desired
        _record_clock_transition(booking, previous, now)
        count += 1
    return count


def _validate_booking_day(starts_at, ends_at):
    if starts_at and ends_at:
        start = timezone.localtime(starts_at) if timezone.is_aware(starts_at) else starts_at
        end = timezone.localtime(ends_at) if timezone.is_aware(ends_at) else ends_at
        if start.date() != end.date():
            raise ValidationError('Бронирование должно начинаться и заканчиваться в один день.')


def save_booking(user, booking):
    creating = booking.pk is None
    if creating:
        require_capability(user, 'bookings_create')
    elif not (can(user, 'bookings_manage') or can(user, 'bookings_create')):
        raise PermissionDenied('Вы не можете редактировать бронирования.')
    now = _booking_now()
    # Commit freshness before an action can fail validation/authorization.
    sync_booking_statuses(now=now)
    with transaction.atomic():
        original = None
        if creating:
            booking.created_by = user
            booking.status = 'planned'
            booking.engineer = None
            booking.montage_completed = False
        else:
            original = StudioBooking.objects.select_for_update().get(pk=booking.pk)
            if not can_edit_booking(user, original):
                raise PermissionDenied('Вы не можете редактировать эту бронь.')
            # Workflow fields are controlled only by dedicated actions.
            booking.created_by_id = original.created_by_id
            booking.status = original.status
            booking.engineer_id = original.engineer_id
            booking.montage_completed = original.montage_completed
            if original.engineer_id and booking.kind != original.kind:
                raise ValidationError('Нельзя менять тип записи после назначения инженера.')
        if not can(user, 'bookings_manage'):
            booking.responsible = user
        if not booking.responsible_id or not booking.responsible.is_active:
            raise ValidationError('Выберите активного ответственного сотрудника.')
        _validate_booking_day(booking.starts_at, booking.ends_at)
        previous = booking.status
        booking.status = _booking_status_at(booking, now)
        booking.full_clean()
        booking.save()
        audit(user, booking, 'Создание брони' if creating else 'Изменение брони')
        if booking.status != previous:
            _record_clock_transition(booking, previous, now)
        if booking.kind == 'video' and (creating or original.kind != 'video'):
            notify_engineers(booking)
        return booking


def set_booking_status(user, booking, status):
    require_capability(user, 'bookings_view')
    now = _booking_now()
    sync_booking_statuses(now=now, booking_ids=[booking.pk])
    with transaction.atomic():
        booking = StudioBooking.objects.select_for_update().get(pk=booking.pk)
        if not can_edit_booking(user, booking):
            raise PermissionDenied('Вы не можете изменять статус этой брони.')
        if status == 'recording':
            raise ValidationError('Запись начинается автоматически по расписанию. Вручную начать запись нельзя.')
        allowed = {'planned': {'completed', 'cancelled'}, 'recording': {'completed', 'cancelled'}}
        if status not in allowed.get(booking.status, set()):
            raise ValidationError('Этот переход статуса недоступен.')
        previous = booking.status
        booking.status = status
        booking.full_clean()
        won = StudioBooking.objects.filter(pk=booking.pk, status=previous).update(status=status, updated_at=now)
        if not won:
            raise ValidationError('Статус уже изменился. Обновите страницу.')
        booking.updated_at = now
        audit(user, booking, f'Статус: {previous} → {status}')
        if status == 'completed':
            _notify_booking_completed(booking, now)
        return booking


def claim_booking(user, booking):
    if role_of(user) != Role.ENGINEER:
        raise PermissionDenied('Откликнуться на съёмку может только инженер.')
    now = _booking_now()
    sync_booking_statuses(now=now, booking_ids=[booking.pk])
    with transaction.atomic():
        booking = StudioBooking.objects.select_for_update().get(pk=booking.pk)
        if booking.kind != 'video' or booking.status in {'completed', 'cancelled'} or booking.engineer_id:
            raise ValidationError('Эта съёмка недоступна для отклика.')
        booking.engineer = user
        booking.save(update_fields=['engineer', 'updated_at'])
        notify([booking.responsible], 'На видеосъёмку назначен инженер', user.get_full_name() or user.username, booking.get_absolute_url())
        audit(user, booking, 'Инженер принял видеосъёмку')
        return booking


def complete_booking_montage(user, booking):
    if role_of(user) not in {Role.DIRECTOR, Role.ENGINEER}:
        raise PermissionDenied('Монтаж может завершить назначенный инженер.')
    now = _booking_now()
    sync_booking_statuses(now=now, booking_ids=[booking.pk])
    with transaction.atomic():
        booking = StudioBooking.objects.select_for_update().get(pk=booking.pk)
        if not (role_of(user) == Role.DIRECTOR or booking.engineer_id == user.pk):
            raise PermissionDenied('Монтаж может завершить назначенный инженер.')
        if booking.kind != 'video' or booking.status != 'completed' or booking.montage_completed:
            raise ValidationError('Сначала завершите видеозапись. Повторная отметка недоступна.')
        booking.montage_completed = True
        booking.save(update_fields=['montage_completed', 'updated_at'])
        audit(user, booking, 'Монтаж студийной видеозаписи завершён')
        notify([booking.responsible], 'Студийная видеозапись смонтирована', booking.title, booking.get_absolute_url())
        return booking


def _validate_event_people(event):
    for field, role in [('reporter', Role.TV_REPORTER), ('camera', Role.OPERATOR), ('photographer', Role.PHOTOGRAPHER), ('editor', Role.EDITOR)]:
        employee = getattr(event, field)
        if employee and (not employee.is_active or role_of(employee) != role):
            raise ValidationError({field: 'Выберите активного сотрудника с соответствующей ролью.'})


def _editor_notification(event):
    if event.editor_id:
        available_at = max(timezone.now(), event.shooting_completed_at + timedelta(minutes=30))
        notify([event.editor], 'Материалы переданы на монтаж', event.title, event.get_absolute_url(), available_at)


@transaction.atomic
def save_event(user, event):
    creating = event.pk is None
    old = None
    if creating:
        require_capability(user, 'events_create')
        event.created_by = user
        event.status = 'planned'
        event.shooting_completed_at = None
    else:
        old = Event.objects.select_for_update().get(pk=event.pk)
        if not can_edit_event(user, old):
            raise PermissionDenied('Вы не можете редактировать этот инфоповод.')
        event.created_by_id = old.created_by_id
        event.status = old.status
        event.shooting_completed_at = old.shooting_completed_at
        if old.status in {'approved', 'published'} and role_of(user) not in {Role.DIRECTOR, Role.TV_CHIEF}:
            raise PermissionDenied('Утверждённый материал может изменять только главред ТВ или директор.')
    _validate_event_people(event)
    event.full_clean()
    event.save()
    changed = []
    for field in ['reporter', 'camera', 'photographer', 'editor']:
        assigned = getattr(event, field)
        if assigned and (creating or getattr(old, field + '_id') != assigned.pk):
            changed.append(assigned)
    notify(changed, 'Вас назначили на инфоповод', event.title, event.get_absolute_url())
    for field in ['camera', 'photographer']:
        employee_id = getattr(event, field + '_id')
        if employee_id:
            assignment, _ = EventAssignment.objects.get_or_create(event=event, job=field, defaults={'employee_id': employee_id})
            if assignment.employee_id != employee_id:
                assignment.employee_id = employee_id
                assignment.status = 'assigned'
                assignment.save()
        else:
            EventAssignment.objects.filter(event=event, job=field).delete()
    if creating:
        EventStatusHistory.objects.create(event=event, actor=user, old_status='', new_status='planned')
    elif old.editor_id != event.editor_id and event.status == 'transferred':
        Notification.objects.filter(url=event.get_absolute_url(), title='Материалы переданы на монтаж', is_read=False).delete()
        _editor_notification(event)
    audit(user, event, 'Создание инфоповода' if creating else 'Изменение инфоповода')
    return event


@transaction.atomic
def transition_event(user, event, target_status):
    event = Event.objects.select_for_update().get(pk=event.pk)
    role = role_of(user)
    chief = role in {Role.DIRECTOR, Role.TV_CHIEF}
    authorized = {
        'shot': chief or (role == Role.TV_REPORTER and event.reporter_id == user.pk),
        'transferred': chief or (role == Role.TV_REPORTER and event.reporter_id == user.pk),
        'edited': role == Role.DIRECTOR or (role == Role.EDITOR and event.editor_id == user.pk),
        'approved': chief,
        'published': role in {Role.DIRECTOR, Role.SITE_CHIEF, Role.SITE_EDITOR},
    }
    if not authorized.get(target_status, False):
        raise PermissionDenied('Вы не можете выполнять этот этап для данного материала.')
    following = {'planned': 'shot', 'shot': 'transferred', 'transferred': 'edited', 'edited': 'approved', 'approved': 'published'}
    if following.get(event.status) != target_status:
        raise ValidationError('Этапы выполняются последовательно. Обновите страницу.')
    if target_status == 'transferred':
        if not event.files.exists():
            raise ValidationError('Сначала загрузите файлы мероприятия.')
        if not event.editor_id or not event.editor.is_active:
            raise ValidationError('Перед передачей материалов назначьте активного монтажёра.')
    old_status = event.status
    event.status = target_status
    if target_status == 'shot':
        event.shooting_completed_at = timezone.now()
    event.save(update_fields=['status', 'shooting_completed_at', 'updated_at'])
    EventStatusHistory.objects.create(event=event, actor=user, old_status=old_status, new_status=target_status)
    audit(user, event, f'Этап: {old_status} → {target_status}')
    if target_status == 'transferred':
        _editor_notification(event)
    elif target_status == 'edited':
        notify(users_with_roles(Role.TV_CHIEF, Role.DIRECTOR), 'Видео смонтировано — требуется одобрение', event.title, event.get_absolute_url())
    elif target_status == 'approved':
        notify(users_with_roles(Role.SITE_CHIEF, Role.SITE_EDITOR), 'Материал одобрен и готов к публикации', event.title, event.get_absolute_url())
    return event


@transaction.atomic
def set_assignment_status(user, event, status):
    if role_of(user) not in {Role.OPERATOR, Role.PHOTOGRAPHER}:
        raise PermissionDenied('Только участник съёмочной группы может менять свою задачу.')
    assignment = EventAssignment.objects.select_for_update().filter(event=event, employee=user).first()
    if assignment is None:
        raise PermissionDenied('Вы не назначены на эту работу.')
    if status not in {'working', 'completed'} or assignment.status == 'completed' or (assignment.status == 'working' and status == 'working'):
        raise ValidationError('Этот статус работы недоступен.')
    assignment.status = status
    assignment.save(update_fields=['status', 'updated_at'])
    audit(user, event, f'{assignment.get_job_display()}: {assignment.get_status_display()}')
    return assignment


def _validate_upload(upload):
    size = upload.size
    ext = Path(upload.name).suffix.lower().lstrip('.')
    if not size or size > settings.MAX_UPLOAD_SIZE:
        raise ValidationError(f'Файл должен быть непустым и не больше {settings.MAX_UPLOAD_MB} МБ.')
    if ext not in settings.ALLOWED_UPLOAD_EXTENSIONS:
        raise ValidationError('Этот тип файла не разрешён. Выберите документ, изображение, аудио, видео или ZIP-архив.')
    # MIME declarations are untrusted. Refuse executable/web content regardless of its declared MIME.
    head = upload.read(512)
    upload.seek(0)
    lowered = head.lstrip().lower()
    if head.startswith((b'MZ', b'\x7fELF', b'#!', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf')) or lowered.startswith((b'<!doctype html', b'<html', b'<script', b'<?php', b'<svg')):
        raise ValidationError('Исполняемые файлы и активное веб-содержимое запрещены.')


@transaction.atomic
def save_upload(user, uploaded_file):
    require_capability(user, 'files_upload')
    if uploaded_file.pk:
        raise PermissionDenied('Заменять ранее загруженные файлы нельзя.')
    if uploaded_file.event_id and not can_view_event_materials(user, uploaded_file.event):
        raise PermissionDenied('Вы не имеете доступа к материалам этого мероприятия.')
    if uploaded_file.event_id and uploaded_file.event.status in {'approved', 'published'} and role_of(user) not in {Role.DIRECTOR, Role.TV_CHIEF}:
        raise PermissionDenied('После одобрения исходные материалы может менять только главред ТВ или директор.')
    _validate_upload(uploaded_file.file)
    uploaded_file.uploaded_by = user
    uploaded_file.original_name = Path(uploaded_file.file.name.replace('\\', '/')).name[:250]
    uploaded_file.size = uploaded_file.file.size
    if not uploaded_file.title:
        uploaded_file.title = uploaded_file.original_name[:220]
    uploaded_file.full_clean()
    uploaded_file.save()
    audit(user, uploaded_file, 'Загрузка файла')
    return uploaded_file


@transaction.atomic
def delete_upload(user, uploaded_file):
    uploaded_file = UploadedFile.objects.select_for_update().select_related('event').get(pk=uploaded_file.pk)
    if not can_view_file(user, uploaded_file) or not (uploaded_file.uploaded_by_id == user.pk or role_of(user) == Role.DIRECTOR):
        raise PermissionDenied('Вы можете удалять только собственные доступные файлы.')
    if uploaded_file.event_id and uploaded_file.event.status in {'transferred', 'edited', 'approved', 'published'} and role_of(user) != Role.DIRECTOR:
        raise ValidationError('Материалы, уже переданные на монтаж, сохраняются для производственного процесса.')
    storage, name = uploaded_file.file.storage, uploaded_file.file.name
    audit(user, uploaded_file, 'Удаление файла')
    uploaded_file.delete()
    transaction.on_commit(lambda: storage.delete(name))


@transaction.atomic
def save_proposal(user, proposal):
    require_capability(user, 'proposals_create')
    if proposal.pk:
        raise PermissionDenied('Отправленное предложение нельзя изменять.')
    proposal.author = user
    proposal.status = 'pending'
    proposal.reviewed_by = None
    proposal.event = None
    proposal.booking = None
    if proposal.kind == 'booking':
        _validate_booking_day(proposal.starts_at, proposal.ends_at)
    proposal.full_clean()
    proposal.save()
    recipients = users_with_roles(Role.DIRECTOR, Role.TV_CHIEF, *([Role.RADIO_CHIEF] if proposal.kind == 'booking' else []))
    notify(recipients, 'Новое предложение продюсера', proposal.title, proposal.get_absolute_url())
    audit(user, proposal, 'Предложение отправлено')
    return proposal


@transaction.atomic
def review_proposal(user, proposal, approve):
    proposal = Proposal.objects.select_for_update().get(pk=proposal.pk)
    allowed = {Role.DIRECTOR, Role.TV_CHIEF} | ({Role.RADIO_CHIEF} if proposal.kind == 'booking' else set())
    if role_of(user) not in allowed or proposal.author_id == user.pk:
        raise PermissionDenied('Вы не можете рассматривать это предложение.')
    if proposal.status != 'pending':
        raise ValidationError('Предложение уже рассмотрено.')
    if approve:
        if proposal.kind == 'event':
            proposal.event = save_event(user, Event(title=proposal.title, description=proposal.description, starts_at=proposal.starts_at, ends_at=proposal.ends_at))
        else:
            proposal.booking = save_booking(user, StudioBooking(title=proposal.title, description=proposal.description, starts_at=proposal.starts_at, ends_at=proposal.ends_at, studio=proposal.studio, kind=proposal.booking_kind, responsible=proposal.author))
    proposal.status = 'approved' if approve else 'rejected'
    proposal.reviewed_by = user
    proposal.save(update_fields=['status', 'reviewed_by', 'event', 'booking', 'updated_at'])
    notify([proposal.author], 'Предложение рассмотрено', f'{proposal.title}: {proposal.get_status_display()}', proposal.get_absolute_url())
    audit(user, proposal, proposal.get_status_display())
    return proposal


@transaction.atomic
def save_employee(user, target_user, role, department, password=None):
    require_capability(user, 'employees_manage')
    creating = target_user.pk is None
    if role not in allowed_employee_roles(user):
        raise PermissionDenied('Вы не можете назначать эту роль.')
    if department.slug != ROLE_DEPARTMENTS.get(role):
        raise ValidationError('Выбранная роль не соответствует подразделению.')
    if role_of(user) != Role.DIRECTOR and department.pk != user.employee.department_id:
        raise PermissionDenied('Вы можете создавать сотрудников только своего подразделения.')
    if not creating:
        from django.contrib.auth import get_user_model
        original = get_user_model().objects.select_for_update().get(pk=target_user.pk)
        if not can_manage_employee(user, original):
            raise PermissionDenied('Вы не можете изменять этого сотрудника.')
        if target_user.pk == user.pk and (role != Role.DIRECTOR or not target_user.is_active):
            raise ValidationError('Директор не может отключить или понизить собственный аккаунт.')
        target_user.password = original.password
    elif not password:
        raise ValidationError('Для нового сотрудника задайте пароль.')
    # Django staff/superuser flags are never controlled by this interface.
    target_user.is_staff = False
    target_user.is_superuser = False
    if password:
        password_validation.validate_password(password, target_user)
        target_user.set_password(password)
    target_user.full_clean()
    target_user.save()
    assign_role(target_user, role, department)
    audit(user, target_user, 'Создание сотрудника' if creating else 'Изменение сотрудника')
    return target_user
