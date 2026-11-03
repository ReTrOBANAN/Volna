from datetime import datetime, time

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import (
    BroadcastShift, Department, EmployeeProfile, Event, Proposal,
    StudioBooking, UploadedFile,
)
from .permissions import (
    allowed_employee_roles, can_view_event_materials, role_of, visible_events,
)

User = get_user_model()


class RussianAuthenticationForm(AuthenticationForm):
    username = forms.CharField(label="Логин", widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username"}))
    password = forms.CharField(label="Пароль", strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))
    error_messages = {
        "invalid_login": "Неверный логин или пароль. Проверьте введённые данные.",
        "inactive": "Аккаунт отключён. Обратитесь к руководителю.",
    }


class EmployeeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.get_full_name() or obj.username


class LocalDateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%dT%H:%M", **kwargs)


class ScheduleForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("starts_at", "ends_at"):
            if name in self.fields:
                self.fields[name].widget = LocalDateTimeInput()
                self.fields[name].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"]
        for field in self.fields.values():
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs["rows"] = 4

    def clean(self):
        data = super().clean()
        if data.get("starts_at") and data.get("ends_at") and data["ends_at"] <= data["starts_at"]:
            self.add_error("ends_at", "Окончание должно быть позже начала.")
        return data


class ShiftForm(forms.ModelForm):
    employee = EmployeeChoiceField(queryset=User.objects.none(), label="Сотрудник")
    date = forms.DateField(
        label="Дата эфира",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    start_time = forms.TimeField(
        label="От", input_formats=["%H:%M", "%H:%M:%S"],
        widget=forms.TimeInput(format="%H:%M", attrs={"type": "time", "step": "60"}),
    )
    end_time = forms.TimeField(
        label="До", input_formats=["%H:%M", "%H:%M:%S"],
        widget=forms.TimeInput(format="%H:%M", attrs={"type": "time", "step": "60"}),
    )
    class Meta:
        model = BroadcastShift
        fields = ["employee", "date", "start_time", "end_time", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        help_texts = {"notes": "Необязательно"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = User.objects.filter(
            is_active=True, employee__department__slug="radio",
        ).order_by("first_name", "username")
        if self.instance.pk:
            start = timezone.localtime(self.instance.starts_at)
            self.initial.update(
                date=start.date(), start_time=start.time(),
                end_time=timezone.localtime(self.instance.ends_at).time(),
            )
        else:
            self.initial.setdefault("date", timezone.localdate())
            self.initial.setdefault("start_time", time(10))
            self.initial.setdefault("end_time", time(13))

    def clean(self):
        data = super().clean()
        if self.data.get("end_date"):
            self.add_error(None, "Эфир должен начинаться и заканчиваться в один день.")
        day = data.get("date")
        if not day or any(field not in data for field in ["start_time", "end_time"]):
            return data
        # DateTimeField applies Django's timezone/DST validation to local inputs.
        datetimes = {}
        for field in ["start_time", "end_time"]:
            try:
                datetimes[field] = forms.DateTimeField().clean(datetime.combine(day, data[field]))
            except ValidationError as exc:
                self.add_error(field, exc)
        if len(datetimes) != 2:
            return data
        if datetimes["end_time"] <= datetimes["start_time"]:
            self.add_error("end_time", "Время «До» должно быть позже времени «От». Эфир проходит в пределах одного дня.")
            return data
        self.instance.starts_at = datetimes["start_time"]
        self.instance.ends_at = datetimes["end_time"]
        return data


class BookingForm(forms.ModelForm):
    date = forms.DateField(
        label="Дата записи",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "min": "1970-01-01", "max": "2200-12-31"}),
    )
    start_time = forms.TimeField(
        label="От", input_formats=["%H:%M", "%H:%M:%S"],
        widget=forms.TimeInput(format="%H:%M", attrs={"type": "time", "step": "60"}),
    )
    end_time = forms.TimeField(
        label="До", input_formats=["%H:%M", "%H:%M:%S"],
        widget=forms.TimeInput(format="%H:%M", attrs={"type": "time", "step": "60"}),
    )
    class Meta:
        model = StudioBooking
        fields = ["title", "description", "studio", "date", "start_time", "end_time", "kind"]
        labels = {
            "title": "Название записи", "description": "Описание", "studio": "Студия",
            "kind": "Тип записи",
        }
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            start = timezone.localtime(self.instance.starts_at)
            end = timezone.localtime(self.instance.ends_at)
            self.initial.update(
                date=start.date(), start_time=start.time(), end_time=end.time(),
            )
        else:
            self.initial.setdefault("date", timezone.localdate())
            self.initial.setdefault("start_time", time(10))
            self.initial.setdefault("end_time", time(13))

    @staticmethod
    def _calendar_day(day):
        if day and not 1970 <= day.year <= 2200:
            raise ValidationError("Выберите дату с 1970 по 2200 год.")
        return day

    def clean_date(self):
        return self._calendar_day(self.cleaned_data["date"])

    def map_model_errors(self, error):
        """Model/service errors refer to stored datetimes; the form uses split fields."""
        if not hasattr(error, "error_dict"):
            return error
        mapped = {}
        for field, errors in error.error_dict.items():
            target = {"starts_at": "start_time", "ends_at": "end_time"}.get(field, field)
            if target not in self.fields:
                target = "__all__"
            mapped.setdefault(target, []).extend(errors)
        return ValidationError(mapped)

    def _update_errors(self, errors):
        super()._update_errors(self.map_model_errors(errors))

    def clean(self):
        data = super().clean()
        if self.data.get("end_date"):
            self.add_error(None, "Бронирование должно начинаться и заканчиваться в один день.")
        day = data.get("date")
        if not day or any(field not in data for field in ("start_time", "end_time")):
            return data
        datetimes = {}
        for field in ("start_time", "end_time"):
            try:
                datetimes[field] = forms.DateTimeField().clean(datetime.combine(day, data[field]))
            except ValidationError as exc:
                self.add_error(field, exc)
        if len(datetimes) != 2:
            return data
        if datetimes["end_time"] <= datetimes["start_time"]:
            self.add_error("end_time", "Время «До» должно быть позже времени «От». Бронирование проходит в пределах одного дня.")
            return data
        self.instance.starts_at = datetimes["start_time"]
        self.instance.ends_at = datetimes["end_time"]
        return data


class EventForm(ScheduleForm):
    reporter = EmployeeChoiceField(queryset=User.objects.none(), label="Корреспондент", required=False)
    camera = EmployeeChoiceField(queryset=User.objects.none(), label="Оператор", required=False)
    photographer = EmployeeChoiceField(queryset=User.objects.none(), label="Фотограф", required=False)
    editor = EmployeeChoiceField(queryset=User.objects.none(), label="Монтажёр", required=False)

    class Meta:
        model = Event
        fields = ["title", "description", "location", "starts_at", "ends_at", "reporter", "camera", "photographer", "editor"]
        labels = {
            "title": "Название инфоповода", "description": "Описание", "location": "Место проведения",
            "starts_at": "Начало мероприятия", "ends_at": "Окончание мероприятия",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field, role in {"reporter": "tv_reporter", "camera": "operator", "photographer": "photographer", "editor": "editor"}.items():
            self.fields[field].queryset = User.objects.filter(
                is_active=True, employee__role=role,
            ).order_by("first_name", "username")


class UploadForm(forms.ModelForm):
    class Meta:
        model = UploadedFile
        fields = ["title", "file", "event"]
        labels = {"title": "Название", "file": "Файл", "event": "Инфоповод (необязательно)"}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["event"].queryset = Event.objects.filter(pk__in=[
            event.pk for event in visible_events(user) if can_view_event_materials(user, event)
            and (event.status not in ("approved", "published") or role_of(user) in ("director", "tv_chief"))
        ])
        self.fields["event"].required = False


class ProposalForm(ScheduleForm):
    class Meta:
        model = Proposal
        fields = ["kind", "title", "description", "starts_at", "ends_at", "studio", "booking_kind"]
        labels = {
            "kind": "Тип предложения", "title": "Название", "description": "Описание",
            "starts_at": "Предлагаемое начало", "ends_at": "Предлагаемое окончание",
            "studio": "Студия", "booking_kind": "Тип записи",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["studio"].required = False
        self.fields["studio"].help_text = "Для предложения брони выберите студию."

    def clean(self):
        data = super().clean()
        if data.get("kind") == "booking" and not data.get("studio"):
            self.add_error("studio", "Выберите студию для бронирования.")
        return data


class EmployeeForm(forms.Form):
    full_name = forms.CharField(label="ФИО", max_length=150)
    username = forms.CharField(label="Логин", max_length=150)
    password = forms.CharField(
        label="Пароль", required=False, strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="При редактировании оставьте пустым, чтобы сохранить пароль.",
    )
    department = forms.ModelChoiceField(queryset=Department.objects.none(), label="Подразделение")
    role = forms.ChoiceField(label="Роль")
    is_active = forms.BooleanField(label="Аккаунт активен", required=False, initial=True)

    def __init__(self, *args, user, instance=None, **kwargs):
        self.target = instance
        initial = kwargs.setdefault("initial", {})
        if instance:
            initial.update(full_name=instance.get_full_name(), username=instance.username, is_active=instance.is_active)
            if hasattr(instance, "employee"):
                initial.update(department=instance.employee.department_id, role=instance.employee.role)
        super().__init__(*args, **kwargs)
        allowed = set(allowed_employee_roles(user))
        self.fields["role"].choices = [(value, label) for value, label in EmployeeProfile._meta.get_field("role").choices if value in allowed]
        self.fields["department"].queryset = Department.objects.all()
        if role_of(user) != "director":
            self.fields["department"].queryset = Department.objects.filter(pk=user.employee.department_id)
        if not instance:
            self.fields["password"].required = True
        self.fields["username"].validators.extend(User._meta.get_field("username").validators)

    def clean_username(self):
        username = self.cleaned_data["username"]
        qs = User.objects.filter(username=username)
        if self.target:
            qs = qs.exclude(pk=self.target.pk)
        if qs.exists():
            raise ValidationError("Этот логин уже занят.")
        return username

    def clean_password(self):
        password = self.cleaned_data.get("password")
        if password:
            candidate = self.target or User(username=self.data.get("username", ""), first_name=self.data.get("full_name", ""))
            validate_password(password, user=candidate)
        return password


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "email"]
        labels = {"first_name": "ФИО", "email": "Электронная почта"}
