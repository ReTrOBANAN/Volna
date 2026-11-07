import calendar
from datetime import datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_POST

from . import services
from .forms import BookingForm, EmployeeForm, EventForm, ProfileForm, ProposalForm, ShiftForm, UploadForm
from .models import BroadcastShift, Event, EventAssignment, Notification, Proposal, Studio, StudioBooking, UploadedFile
from .permissions import (
    can, can_edit_booking, can_edit_event, can_manage_employee,
    can_view_event_materials, can_view_file, require_capability, role_of,
    visible_events, visible_files,
)

User = get_user_model()


def _calendar_date(value):
    try:
        day = parse_date(value or "")
        return day if day and 1970 <= day.year <= 2200 else None
    except (ValueError, TypeError):
        return None


def _selected_date(request, key="date"):
    return _calendar_date(request.GET.get(key)) or timezone.localdate()


def _aware_day(day):
    return timezone.make_aware(datetime.combine(day, time.min))


def _form_page(request, form, title, cancel_name, *, cancel_args=None, submit_label="Сохранить"):
    return render(request, "newsroom/form.html", {
        "form": form, "title": title, "page_title": title, "submit_label": submit_label,
        "cancel_url": reverse(f"newsroom:{cancel_name}", args=cancel_args),
    })


def _add_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, exc)


@login_required
def dashboard(request):
    now = timezone.now()
    events = visible_events(request.user) if can(request.user, "events_view") else Event.objects.none()
    bookings = StudioBooking.objects.select_related("studio", "responsible") if can(request.user, "bookings_view") else StudioBooking.objects.none()
    assigned = events.filter(Q(reporter=request.user) | Q(camera=request.user) | Q(photographer=request.user) | Q(editor=request.user)).exclude(status="published")
    assigned_bookings = bookings.filter(
        Q(responsible=request.user, status__in=["planned", "recording"])
        | Q(engineer=request.user, montage_completed=False, status__in=["planned", "recording", "completed"])
    )
    notifications = Notification.objects.filter(recipient=request.user, available_at__lte=now).order_by("-created_at")[:5]
    return render(request, "newsroom/dashboard.html", {
        "page_title": "Рабочий стол", "today": timezone.localdate(),
        "upcoming_events": events.filter(ends_at__gte=now).order_by("starts_at")[:5],
        "upcoming_bookings": bookings.filter(ends_at__gte=now).exclude(status="cancelled").order_by("starts_at")[:5],
        "assigned_events": assigned.order_by("starts_at")[:6],
        "assigned_bookings": assigned_bookings.order_by("starts_at")[:6], "recent_notifications": notifications,
        "event_count": events.filter(ends_at__gte=now).count(),
        "booking_count": bookings.filter(ends_at__gte=now).exclude(status="cancelled").count(),
        "task_count": assigned.count() + assigned_bookings.count(),
        "file_count": visible_files(request.user).count() if can(request.user, "files_view") else 0,
    })


@login_required
def shifts(request):
    require_capability(request.user, "shifts_view")
    today = timezone.localdate()
    day = _calendar_date(request.GET.get("date")) or today
    start = day - timedelta(days=day.weekday())
    end = start + timedelta(days=14)
    entries = BroadcastShift.objects.filter(starts_at__gte=_aware_day(start), starts_at__lt=_aware_day(end)).select_related("employee").order_by("starts_at")
    days = []
    for offset in range(14):
        current = start + timedelta(days=offset)
        days.append({
            "date": current, "is_today": current == today, "is_selected": current == day,
            "shifts": [s for s in entries if timezone.localtime(s.starts_at).date() == current],
        })
    return render(request, "newsroom/shifts.html", {
        "page_title": "Расписание эфиров", "days": days, "start": start, "end": end - timedelta(days=1),
        "previous_date": start - timedelta(days=14), "next_date": end, "selected_date": day,
    })


def _shift_schedule(day, exclude=None):
    entries = BroadcastShift.objects.filter(
        starts_at__gte=_aware_day(day), starts_at__lt=_aware_day(day + timedelta(days=1)),
    ).select_related("employee").order_by("starts_at", "ends_at", "pk")
    if exclude is not None:
        entries = entries.exclude(pk=exclude)
    return [{
        "id": entry.pk, "employee_id": entry.employee_id,
        "employee": entry.employee.get_full_name() or entry.employee.username,
        "start_time": timezone.localtime(entry.starts_at).strftime("%H:%M"),
        "end_time": timezone.localtime(entry.ends_at).strftime("%H:%M"),
        "notes": entry.notes,
    } for entry in entries]


@login_required
@require_GET
def shift_day_schedule(request):
    require_capability(request.user, "shifts_view")
    day = _calendar_date(request.GET.get("date"))
    if day is None:
        return JsonResponse({"error": "Укажите корректную дату расписания."}, status=400)
    excluded = request.GET.get("exclude", "")
    if excluded and (not excluded.isascii() or not excluded.isdigit() or len(excluded) > 18):
        return JsonResponse({"error": "Некорректный идентификатор эфира."}, status=400)
    response = JsonResponse({
        "date": day.isoformat(), "shifts": _shift_schedule(day, int(excluded) if excluded else None),
    }, json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def shift_create(request, pk=None):
    require_capability(request.user, "shifts_manage")
    shift = get_object_or_404(BroadcastShift, pk=pk) if pk else None
    selected_day = timezone.localtime(shift.starts_at).date() if shift else _selected_date(request)
    explicit_period = _calendar_date(request.GET.get("period"))
    period = explicit_period or selected_day
    period_start = period - timedelta(days=period.weekday())
    form = ShiftForm(request.POST if request.method == "POST" else None, instance=shift, initial={"date": selected_day})
    if request.method == "POST" and form.is_valid():
        entry = form.save(commit=False)
        if not entry.pk:
            entry.created_by = request.user
        entry.full_clean()
        entry.save()
        saved_day = timezone.localtime(entry.starts_at).date()
        return_day = period_start if explicit_period and period_start <= saved_day < period_start + timedelta(days=14) else saved_day
        return redirect(f"{reverse('newsroom:shifts')}?date={return_day.isoformat()}")
    title = "Изменить эфир" if pk else "Добавить эфир"
    preview_day = (_calendar_date(form.data.get("date")) if form.is_bound else None) or selected_day
    preview_employee = form.instance.employee if form.instance.employee_id else None
    return render(request, "newsroom/shift_form.html", {
        "form": form, "title": title, "page_title": title,
        "submit_label": "Сохранить изменения" if pk else "Добавить эфир",
        "cancel_url": f"{reverse('newsroom:shifts')}?date={period.isoformat()}",
        "day_schedule": _shift_schedule(preview_day, pk), "schedule_date": preview_day,
        "schedule_url": reverse("newsroom:shift_day_schedule"), "editing_shift_id": pk,
        "draft_start_time": form.fields["start_time"].widget.format_value(form["start_time"].value()) or "--:--",
        "draft_end_time": form.fields["end_time"].widget.format_value(form["end_time"].value()) or "--:--",
        "draft_employee": (preview_employee.get_full_name() or preview_employee.username) if preview_employee else ("Изменяемый эфир" if pk else "Новый эфир"),
    })


@login_required
@require_POST
def shift_delete(request, pk):
    require_capability(request.user, "shifts_manage")
    get_object_or_404(BroadcastShift, pk=pk).delete()
    return redirect("newsroom:shifts")


@login_required
def bookings(request):
    require_capability(request.user, "bookings_view")
    today = timezone.localdate()
    day = _calendar_date(request.GET.get("date")) or today
    mode = "day" if request.GET.get("view") == "day" else "week"
    start = day if mode == "day" else day - timedelta(days=day.weekday())
    length = 1 if mode == "day" else 7
    end = start + timedelta(days=length)
    entries = StudioBooking.objects.filter(starts_at__lt=_aware_day(end), ends_at__gt=_aware_day(start)).exclude(status="cancelled").select_related("studio", "responsible", "engineer").order_by("starts_at")
    studios = []
    for studio in Studio.objects.order_by("pk"):
        studio_bookings = [b for b in entries if b.studio_id == studio.pk]
        studios.append({"studio": studio, "bookings": studio_bookings, "days": [{
            "date": start + timedelta(days=i),
            "is_today": start + timedelta(days=i) == today,
            "bookings": [b for b in studio_bookings if b.starts_at < _aware_day(start + timedelta(days=i + 1)) and b.ends_at > _aware_day(start + timedelta(days=i))],
        } for i in range(length)]})
    return render(request, "newsroom/bookings.html", {
        "page_title": "Бронирование студий", "studios": studios, "bookings": entries,
        "days": [start + timedelta(days=i) for i in range(length)], "start": start,
        "end": end - timedelta(days=1), "view_mode": mode, "selected_date": day,
        "previous_date": start - timedelta(days=length), "next_date": end,
        "booking_status_watch": _booking_watch(entries),
    })


def _booking_status_rows(entries):
    return [{
        "id": entry.pk, "status": entry.status, "engineer_id": entry.engineer_id,
        "starts_at": entry.starts_at.isoformat(), "ends_at": entry.ends_at.isoformat(),
    } for entry in entries]


def _booking_watch(entries):
    return {
        "url": reverse("newsroom:booking_statuses"), "server_now": timezone.now().isoformat(),
        "bookings": _booking_status_rows(entry for entry in entries if entry.status in ("planned", "recording")),
    }


@login_required
@require_GET
def booking_statuses(request):
    require_capability(request.user, "bookings_view")
    values = request.GET.get("ids", "").split(",") if request.GET.get("ids") else []
    ids = [_booking_positive_id(value) for value in values] if len(values) <= 1000 else [None]
    now = timezone.now()
    if any(value is None for value in ids):
        payload, status = {"error": "Укажите корректные идентификаторы бронирований."}, 400
    else:
        services.sync_booking_statuses(now=now, booking_ids=ids)
        entries = StudioBooking.objects.filter(pk__in=ids).exclude(status="cancelled").order_by("pk")
        payload, status = {"server_now": now.isoformat(), "bookings": _booking_status_rows(entries)}, 200
    response = JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "private, no-store"
    return response


def _booking_positive_id(value):
    value = str(value or "")
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 18 and int(value) > 0 else None


def _booking_studio(value):
    studio_id = _booking_positive_id(value)
    return Studio.objects.filter(pk=studio_id).first() if studio_id else None


def _booking_schedule(day, studio_id, exclude=None):
    day_start, day_end = _aware_day(day), _aware_day(day + timedelta(days=1))
    entries = StudioBooking.objects.filter(
        studio_id=studio_id, starts_at__lt=day_end, ends_at__gt=day_start,
    ).exclude(status="cancelled").select_related("responsible").order_by("starts_at", "ends_at", "pk")
    if exclude is not None:
        entries = entries.exclude(pk=exclude)
    schedule = []
    for entry in entries:
        start, end = timezone.localtime(entry.starts_at), timezone.localtime(entry.ends_at)
        schedule.append({
            "id": entry.pk, "title": entry.title,
            "responsible": entry.responsible.get_full_name() or entry.responsible.username,
            "responsible_id": entry.responsible_id,
            "kind": entry.kind, "kind_label": entry.get_kind_display(),
            "engineer_id": entry.engineer_id,
            "status": entry.status, "status_label": entry.get_status_display(),
            "start_time": start.strftime("%H:%M"), "end_time": end.strftime("%H:%M"),
            "starts_at": start.isoformat(), "ends_at": end.isoformat(),
            "start_label": start.strftime("%H:%M" if start.date() == day else "%d.%m · %H:%M"),
            "end_label": end.strftime("%H:%M" if end.date() == day else "%d.%m · %H:%M"),
            "start_minute": 0 if entry.starts_at <= day_start else start.hour * 60 + start.minute,
            "end_minute": 1440 if entry.ends_at >= day_end else min(1440, end.hour * 60 + end.minute + int(bool(end.second or end.microsecond))),
            "is_blocking": entry.status in ("planned", "recording"),
        })
    return schedule


@login_required
@require_GET
def booking_day_schedule(request):
    require_capability(request.user, "bookings_view")
    day = _calendar_date(request.GET.get("date"))
    studio = _booking_studio(request.GET.get("studio"))
    raw_exclude = request.GET.get("exclude", "")
    exclude = _booking_positive_id(raw_exclude) if raw_exclude else None
    error = None
    if day is None:
        error = "Укажите корректную дату расписания."
    elif studio is None:
        error = "Выберите существующую студию."
    elif raw_exclude and exclude is None:
        error = "Некорректный идентификатор бронирования."
    payload = {"error": error} if error else {
        "date": day.isoformat(), "studio": studio.pk,
        "bookings": _booking_schedule(day, studio.pk, exclude),
    }
    response = JsonResponse(payload, status=400 if error else 200, json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def booking_create(request, pk=None):
    booking = get_object_or_404(StudioBooking, pk=pk) if pk else None
    if booking:
        if not can_edit_booking(request.user, booking):
            raise PermissionDenied
    else:
        require_capability(request.user, "bookings_create")
    selected_day = timezone.localtime(booking.starts_at).date() if booking else _selected_date(request)
    initial_studio = booking.studio if booking else (_booking_studio(request.GET.get("studio")) or Studio.objects.order_by("pk").first())
    mode = "day" if request.GET.get("view") == "day" else "week"
    explicit_period = _calendar_date(request.GET.get("period"))
    period = explicit_period or selected_day
    period_start = period if mode == "day" else period - timedelta(days=period.weekday())
    period_end = period_start + timedelta(days=1 if mode == "day" else 7)
    initial = {"date": selected_day}
    if initial_studio:
        initial["studio"] = initial_studio.pk
    form = BookingForm(request.POST if request.method == "POST" else None, instance=booking, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            booking = services.save_booking(request.user, form.save(commit=False))
        except ValidationError as exc:
            _add_error(form, form.map_model_errors(exc))
        else:
            saved_day = timezone.localtime(booking.starts_at).date()
            return_day = period_start if explicit_period and period_start <= saved_day < period_end else saved_day
            return redirect(f"{reverse('newsroom:bookings')}?date={return_day.isoformat()}&view={mode}")
    preview_day = (_calendar_date(form.data.get("date")) if form.is_bound else None) or selected_day
    preview_studio = _booking_studio(form.data.get("studio")) if form.is_bound else initial_studio
    creator = booking.created_by if booking else request.user
    title = "Изменить бронирование" if pk else "Забронировать студию"
    draft_times = {}
    for name in ("start_time", "end_time"):
        try:
            value = form.fields[name].clean(form[name].value())
        except ValidationError:
            draft_times[name] = "--:--"
        else:
            draft_times[name] = value.strftime("%H:%M")
    day_schedule = _booking_schedule(preview_day, preview_studio.pk, pk) if preview_studio else []
    return render(request, "newsroom/booking_form.html", {
        "form": form, "title": title, "page_title": title,
        "submit_label": "Сохранить изменения" if pk else "Забронировать",
        "cancel_url": f"{reverse('newsroom:bookings')}?date={period.isoformat()}&view={mode}",
        "day_schedule": day_schedule,
        "schedule_date": preview_day, "schedule_studio": preview_studio,
        "schedule_url": reverse("newsroom:booking_day_schedule"), "editing_booking_id": pk,
        "draft_title": str(form["title"].value() or "").strip() or ("Изменяемая запись" if pk else "Новая запись"),
        "draft_responsible": creator.get_full_name() or creator.username,
        "draft_start_time": draft_times["start_time"], "draft_end_time": draft_times["end_time"],
        "draft_start_label": draft_times["start_time"],
        "draft_end_label": draft_times["end_time"],
        "draft_kind_label": dict(StudioBooking.Kind.choices).get(form["kind"].value(), "Тип записи не выбран"),
        "booking_status_watch": {
            "url": reverse("newsroom:booking_statuses"), "server_now": timezone.now().isoformat(),
            "bookings": day_schedule,
        },
    })


@login_required
def booking_detail(request, pk):
    require_capability(request.user, "bookings_view")
    booking = get_object_or_404(StudioBooking.objects.select_related("studio", "responsible", "engineer", "created_by"), pk=pk)
    editable = can_edit_booking(request.user, booking)
    actions = []
    if editable and booking.status == "recording":
        actions.append({"action": "status", "status": "completed", "label": "Завершить запись"})
    if role_of(request.user) == "engineer" and booking.kind == "video" and not booking.engineer_id and booking.status in ("planned", "recording"):
        actions.append({"action": "claim", "label": "Взять бронь в работу"})
    if booking.kind == "video" and booking.status == "completed" and not booking.montage_completed and (booking.engineer_id == request.user.pk or role_of(request.user) == "director"):
        actions.append({"action": "montage", "label": "Отметить «Смонтировано»"})
    return render(request, "newsroom/booking_detail.html", {
        "page_title": booking.title, "booking": booking, "can_edit": editable, "actions": actions,
        "booking_status_watch": _booking_watch([booking]),
    })


@login_required
@require_POST
def booking_action(request, pk):
    require_capability(request.user, "bookings_view")
    booking = get_object_or_404(StudioBooking, pk=pk)
    action = request.POST.get("action")
    try:
        if action == "claim":
            services.claim_booking(request.user, booking)
        elif action == "montage":
            services.complete_booking_montage(request.user, booking)
        elif action == "status":
            services.set_booking_status(request.user, booking, request.POST.get("status"))
        else:
            raise ValidationError("Неизвестное действие.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    except StudioBooking.DoesNotExist as exc:
        raise Http404 from exc
    return redirect("newsroom:booking_detail", pk=pk)


@login_required
@require_POST
def booking_delete(request, pk):
    require_capability(request.user, "bookings_view")
    booking = get_object_or_404(StudioBooking, pk=pk)
    day = timezone.localtime(booking.starts_at).date()
    try:
        services.delete_booking(request.user, booking)
    except StudioBooking.DoesNotExist as exc:
        raise Http404 from exc
    return redirect(f"{reverse('newsroom:bookings')}?date={day.isoformat()}&view=week")


@login_required
def events(request):
    require_capability(request.user, "events_view")
    day = _selected_date(request)
    start = day.replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    qs = visible_events(request.user).select_related("reporter", "camera", "editor").filter(starts_at__lt=_aware_day(end), ends_at__gt=_aware_day(start)).order_by("starts_at")
    status = request.GET.get("status", "")
    valid_statuses = {value for value, _ in Event._meta.get_field("status").choices}
    if status in valid_statuses:
        qs = qs.filter(status=status)
    else:
        status = ""
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(start.year, start.month):
        weeks.append([{
            "date": current, "in_month": current.month == start.month,
            "is_today": current == timezone.localdate(),
            "events": [event for event in qs if event.starts_at < _aware_day(current + timedelta(days=1)) and event.ends_at > _aware_day(current)],
        } for current in week])
    return render(request, "newsroom/events.html", {
        "page_title": "Инфоповоды", "events": qs, "weeks": weeks, "month": start,
        "previous_date": start - timedelta(days=1), "next_date": end,
        "selected_status": status, "status_choices": Event._meta.get_field("status").choices,
    })


@login_required
def event_create(request, pk=None):
    require_capability(request.user, "events_manage" if pk else "events_create")
    event = get_object_or_404(visible_events(request.user), pk=pk) if pk else None
    if event:
        if not can_edit_event(request.user, event):
            raise PermissionDenied
    else:
        require_capability(request.user, "events_create")
    form = EventForm(request.POST or None, instance=event)
    if request.method == "POST" and form.is_valid():
        try:
            event = services.save_event(request.user, form.save(commit=False))
        except ValidationError as exc:
            _add_error(form, exc)
        else:
            return redirect("newsroom:event_detail", pk=event.pk)
    return _form_page(request, form, "Изменить инфоповод" if pk else "Новый инфоповод", "events")


@login_required
def event_detail(request, pk):
    require_capability(request.user, "events_view")
    event = get_object_or_404(visible_events(request.user).select_related("reporter", "camera", "photographer", "editor", "created_by"), pk=pk)
    role = role_of(request.user)
    actions = []
    if role in ("director", "tv_chief") or (role == "tv_reporter" and event.reporter_id == request.user.pk):
        if event.status == "planned":
            actions.append({"action": "shot", "label": "Съёмка завершена"})
        if event.status == "shot":
            actions.append({"action": "transferred", "label": "Передать материалы на монтаж"})
    if event.status == "transferred" and (role == "director" or (role == "editor" and event.editor_id == request.user.pk)):
        actions.append({"action": "edited", "label": "Видео смонтировано"})
    if event.status == "edited" and role in ("director", "tv_chief"):
        actions.append({"action": "approved", "label": "Одобрить материал"})
    if event.status == "approved" and role in ("director", "site_chief", "site_editor"):
        actions.append({"action": "published", "label": "Отметить публикацию"})
    assignment = EventAssignment.objects.filter(event=event, employee=request.user).first()
    material_access = can_view_event_materials(request.user, event)
    return render(request, "newsroom/event_detail.html", {
        "page_title": event.title, "event": event, "can_edit": can_edit_event(request.user, event),
        "actions": actions, "assignment": assignment,
        "event_files": visible_files(request.user).filter(event=event) if material_access else UploadedFile.objects.none(),
        "history": event.history.select_related("actor").order_by("created_at"),
        "can_upload": material_access and can(request.user, "files_upload") and (event.status not in ("approved", "published") or role in ("director", "tv_chief")),
        "can_view_materials": material_access,
    })


@login_required
@require_POST
def event_action(request, pk):
    require_capability(request.user, "events_view")
    event = get_object_or_404(visible_events(request.user), pk=pk)
    action = request.POST.get("action")
    try:
        if action == "assignment":
            services.set_assignment_status(request.user, event, request.POST.get("status"))
        else:
            services.transition_event(request.user, event, action)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect("newsroom:event_detail", pk=pk)


@login_required
def files(request):
    require_capability(request.user, "files_view")
    qs = visible_files(request.user).select_related("uploaded_by", "event").order_by("-created_at")
    search = request.GET.get("q", "").strip()[:200]
    if search:
        qs = qs.filter(Q(title__icontains=search) | Q(original_name__icontains=search))
    return render(request, "newsroom/files.html", {"page_title": "Файлообменник", "files": qs, "search": search})


@login_required
def file_upload(request):
    require_capability(request.user, "files_upload")
    initial = {}
    if request.GET.get("event", "").isdigit():
        initial["event"] = int(request.GET["event"])
    form = UploadForm(request.POST or None, request.FILES or None, user=request.user, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            item = services.save_upload(request.user, form.save(commit=False))
        except ValidationError as exc:
            _add_error(form, exc)
        else:
            if item.event_id:
                return redirect("newsroom:event_detail", pk=item.event_id)
            return redirect("newsroom:files")
    return _form_page(request, form, "Загрузить файл", "files", submit_label="Загрузить")


@login_required
def file_download(request, pk):
    require_capability(request.user, "files_view")
    item = get_object_or_404(UploadedFile.objects.select_related("event"), pk=pk)
    if not can_view_file(request.user, item):
        raise PermissionDenied
    try:
        stream = item.file.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404("Файл не найден в хранилище.")
    response = FileResponse(stream, as_attachment=True, filename=item.original_name)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_POST
def file_delete(request, pk):
    require_capability(request.user, "files_view")
    item = get_object_or_404(UploadedFile, pk=pk)
    try:
        services.delete_upload(request.user, item)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect("newsroom:files")


def _visible_proposals(user):
    qs = Proposal.objects.select_related("author", "reviewed_by", "studio")
    role = role_of(user)
    if role == "producer":
        return qs.filter(author=user)
    if role == "radio_chief":
        return qs.filter(kind="booking")
    return qs


@login_required
def proposals(request):
    require_capability(request.user, "proposals_view")
    return render(request, "newsroom/proposals.html", {
        "page_title": "Предложения продюсера", "proposals": _visible_proposals(request.user).order_by("-created_at"),
    })


@login_required
def proposal_create(request):
    require_capability(request.user, "proposals_create")
    form = ProposalForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            proposal = services.save_proposal(request.user, form.save(commit=False))
        except ValidationError as exc:
            _add_error(form, exc)
        else:
            return redirect("newsroom:proposal_detail", pk=proposal.pk)
    return _form_page(request, form, "Новое предложение", "proposals", submit_label="Отправить предложение")


@login_required
def proposal_detail(request, pk):
    require_capability(request.user, "proposals_view")
    proposal = get_object_or_404(_visible_proposals(request.user), pk=pk)
    role = role_of(request.user)
    review = proposal.status == "pending" and proposal.author_id != request.user.pk and (role in ("director", "tv_chief") or (role == "radio_chief" and proposal.kind == "booking"))
    return render(request, "newsroom/proposal_detail.html", {
        "page_title": proposal.title, "proposal": proposal, "can_review": review,
    })


@login_required
@require_POST
def proposal_review(request, pk):
    require_capability(request.user, "proposals_view")
    proposal = get_object_or_404(_visible_proposals(request.user), pk=pk)
    decision = request.POST.get("decision")
    if decision not in ("approve", "reject"):
        raise PermissionDenied
    try:
        services.review_proposal(request.user, proposal, decision == "approve")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect("newsroom:proposal_detail", pk=pk)


@login_required
def employees(request):
    require_capability(request.user, "employees_manage")
    qs = User.objects.filter(employee__isnull=False).select_related("employee__department").order_by("first_name", "username")
    if role_of(request.user) != "director":
        qs = qs.filter(employee__department=request.user.employee.department)
    employees_list = list(qs)
    for employee in employees_list:
        employee.can_manage = can_manage_employee(request.user, employee)
    return render(request, "newsroom/employees.html", {"page_title": "Сотрудники", "employees": employees_list})


@login_required
def employee_create(request, pk=None):
    require_capability(request.user, "employees_manage")
    target = get_object_or_404(User, pk=pk) if pk else None
    if target and not can_manage_employee(request.user, target):
        raise PermissionDenied
    form = EmployeeForm(request.POST or None, user=request.user, instance=target)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        candidate = target or User()
        candidate.username = data["username"]
        candidate.first_name = data["full_name"]
        candidate.last_name = ""
        candidate.is_active = data["is_active"]
        try:
            services.save_employee(request.user, candidate, data["role"], data["department"], password=data.get("password") or None)
        except ValidationError as exc:
            _add_error(form, exc)
        else:
            return redirect("newsroom:employees")
    return _form_page(request, form, "Изменить сотрудника" if pk else "Новый сотрудник", "employees")


@login_required
def profile(request):
    form = ProfileForm(request.POST or None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("newsroom:profile")
    return render(request, "newsroom/profile.html", {"page_title": "Настройки профиля", "form": form})


@login_required
def password_change(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        return redirect("newsroom:profile")
    return _form_page(request, form, "Изменить пароль", "profile", submit_label="Изменить пароль")


@login_required
def notifications(request):
    return render(request, "newsroom/notifications.html", {
        "page_title": "Уведомления", "notifications": Notification.objects.filter(
            recipient=request.user, available_at__lte=timezone.now(),
        ).order_by("-created_at"),
    })


@login_required
def notification_poll(request):
    qs = Notification.objects.filter(recipient=request.user, available_at__lte=timezone.now())
    response = JsonResponse({"unread_count": qs.filter(is_read=False).count(), "notifications": [{
        "id": item.pk, "title": item.title, "text": item.text,
        "url": item.url, "is_read": item.is_read, "created_at": item.created_at.isoformat(),
    } for item in qs.order_by("-created_at")[:10]]})
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_POST
def notification_read(request, pk):
    notice = get_object_or_404(Notification, pk=pk, recipient=request.user, available_at__lte=timezone.now())
    notice.is_read = True
    notice.save(update_fields=["is_read"])
    return redirect("newsroom:notifications")


@login_required
def news(request):
    require_capability(request.user, "news_view")
    return render(request, "newsroom/news.html", {"page_title": "Новости радио"})
