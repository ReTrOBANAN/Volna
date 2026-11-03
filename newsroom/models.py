import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone


class Role(models.TextChoices):
    DIRECTOR = 'director', 'Директор'
    RADIO_CHIEF = 'radio_chief', 'Главный редактор радио'
    RADIO_REPORTER = 'radio_reporter', 'Корреспондент радио'
    ENGINEER = 'engineer', 'Инженер'
    TV_CHIEF = 'tv_chief', 'Главный редактор телевидения'
    TV_REPORTER = 'tv_reporter', 'Корреспондент телевидения'
    OPERATOR = 'operator', 'Оператор'
    PHOTOGRAPHER = 'photographer', 'Фотограф'
    EDITOR = 'editor', 'Монтажёр'
    SITE_CHIEF = 'site_chief', 'Главный редактор сайта'
    SITE_EDITOR = 'site_editor', 'Редактор сайта'
    PRODUCER = 'producer', 'Продюсер'


class Department(models.Model):
    name = models.CharField('Название', max_length=120)
    slug = models.SlugField(unique=True)

    def __str__(self):
        return self.name


class EmployeeProfile(models.Model):
    Role = Role
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='employee')
    department = models.ForeignKey(Department, on_delete=models.PROTECT, verbose_name='Подразделение')
    role = models.CharField('Роль', max_length=30, choices=Role.choices)

    def __str__(self):
        return self.user.get_full_name() or self.user.username


class Timestamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TimeRange(Timestamped):
    starts_at = models.DateTimeField('Начало')
    ends_at = models.DateTimeField('Окончание')
    demo_key = models.CharField(max_length=100, unique=True, null=True, blank=True, editable=False)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({'ends_at': 'Окончание должно быть позже начала.'})


class BroadcastShift(TimeRange):
    employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='broadcast_shifts', verbose_name='Сотрудник')
    notes = models.TextField('Примечание', blank=True, default='', max_length=2000)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')

    def clean(self):
        super().clean()
        if self.starts_at and self.ends_at:
            start = timezone.localtime(self.starts_at) if timezone.is_aware(self.starts_at) else self.starts_at
            end = timezone.localtime(self.ends_at) if timezone.is_aware(self.ends_at) else self.ends_at
            if start.date() != end.date():
                raise ValidationError('Эфир должен начинаться и заканчиваться в один день.')

    class Meta:
        ordering = ['starts_at']
        constraints = [models.CheckConstraint(condition=Q(ends_at__gt=F('starts_at')), name='shift_positive_duration')]


class Studio(models.Model):
    name = models.CharField('Студия', max_length=80, unique=True)

    def __str__(self):
        return self.name


class StudioBooking(TimeRange):
    class Status(models.TextChoices):
        PLANNED = 'planned', 'Запланировано'
        RECORDING = 'recording', 'Идёт запись'
        COMPLETED = 'completed', 'Завершено'
        CANCELLED = 'cancelled', 'Отменено'

    class Kind(models.TextChoices):
        AUDIO = 'audio', 'Без видео'
        VIDEO = 'video', 'С видео'

    title = models.CharField('Название записи', max_length=220)
    description = models.TextField('Описание', blank=True)
    studio = models.ForeignKey(Studio, on_delete=models.PROTECT, related_name='bookings', verbose_name='Студия')
    kind = models.CharField('Тип записи', max_length=10, choices=Kind.choices, default=Kind.AUDIO)
    responsible = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='responsible_bookings', verbose_name='Ответственный')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_bookings')
    status = models.CharField('Статус', max_length=15, choices=Status.choices, default=Status.PLANNED)
    engineer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='engineering_bookings')
    montage_completed = models.BooleanField('Смонтировано', default=False)

    class Meta:
        ordering = ['starts_at']
        constraints = [models.CheckConstraint(condition=Q(ends_at__gt=F('starts_at')), name='booking_positive_duration')]
        indexes = [models.Index(fields=['studio', 'starts_at', 'ends_at'])]

    def clean(self):
        super().clean()
        if self.studio_id and self.starts_at and self.ends_at and self.status in {'planned', 'recording'}:
            overlap = StudioBooking.objects.filter(studio_id=self.studio_id, status__in=['planned', 'recording'], starts_at__lt=self.ends_at, ends_at__gt=self.starts_at).exclude(pk=self.pk)
            if overlap.exists():
                raise ValidationError('Студия уже занята в выбранное время. Выберите другой интервал или студию.')

    def get_absolute_url(self):
        return reverse('newsroom:booking_detail', args=[self.pk])

    def __str__(self):
        return self.title


class Event(TimeRange):
    class Status(models.TextChoices):
        PLANNED = 'planned', 'Запланировано'
        SHOT = 'shot', 'Съёмка завершена'
        TRANSFERRED = 'transferred', 'Материалы переданы на монтаж'
        EDITED = 'edited', 'Видео смонтировано'
        APPROVED = 'approved', 'Одобрено главным редактором ТВ'
        PUBLISHED = 'published', 'Опубликовано'

    title = models.CharField('Название', max_length=220)
    description = models.TextField('Описание', blank=True)
    location = models.CharField('Место проведения', max_length=250, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_events')
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='reporter_events', verbose_name='Корреспондент')
    camera = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='camera_events', verbose_name='Оператор')
    photographer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='photographer_events', verbose_name='Фотограф')
    editor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='editor_events', verbose_name='Монтажёр')
    status = models.CharField('Рабочий статус', max_length=16, choices=Status.choices, default=Status.PLANNED)
    shooting_completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['starts_at']
        constraints = [models.CheckConstraint(condition=Q(ends_at__gt=F('starts_at')), name='event_positive_duration')]

    @property
    def temporal_status(self):
        now = timezone.now()
        if now < self.starts_at:
            return 'Будущее мероприятие'
        if now < self.ends_at:
            return 'Сейчас проходит'
        return 'Прошедшее мероприятие'

    def get_absolute_url(self):
        return reverse('newsroom:event_detail', args=[self.pk])

    def __str__(self):
        return self.title


class EventAssignment(Timestamped):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='assignments')
    employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='event_assignments')
    job = models.CharField(max_length=20, choices=[('camera', 'Оператор'), ('photographer', 'Фотограф')])
    status = models.CharField(max_length=15, choices=[('assigned', 'Назначено'), ('working', 'В работе'), ('completed', 'Завершено')], default='assigned')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['event', 'job'], name='one_employee_per_event_job')]


class EventStatusHistory(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='history')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    old_status = models.CharField(max_length=16, blank=True, choices=Event.Status.choices)
    new_status = models.CharField(max_length=16, choices=Event.Status.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'pk']


def private_upload_path(instance, filename):
    suffix = Path(filename).suffix.lower()
    return f'uploads/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{suffix}'


class UploadedFile(Timestamped):
    title = models.CharField('Название', max_length=220, blank=True)
    file = models.FileField('Файл', upload_to=private_upload_path)
    original_name = models.CharField(max_length=250)
    size = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='uploads')
    event = models.ForeignKey(Event, on_delete=models.PROTECT, null=True, blank=True, related_name='files', verbose_name='Инфоповод')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title or self.original_name


class Notification(models.Model):
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=220)
    text = models.TextField(blank=True)
    url = models.CharField(max_length=300, blank=True)
    is_read = models.BooleanField(default=False)
    available_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-available_at', '-pk']
        indexes = [models.Index(fields=['recipient', 'is_read', 'available_at'])]


class Proposal(TimeRange):
    class Kind(models.TextChoices):
        EVENT = 'event', 'Инфоповод'
        BOOKING = 'booking', 'Бронь студии'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Ожидает рассмотрения'
        APPROVED = 'approved', 'Одобрено'
        REJECTED = 'rejected', 'Отклонено'

    kind = models.CharField('Тип', max_length=12, choices=Kind.choices)
    title = models.CharField('Название', max_length=220)
    description = models.TextField('Описание', blank=True)
    starts_at = models.DateTimeField('Предлагаемое начало')
    ends_at = models.DateTimeField('Предлагаемое окончание')
    studio = models.ForeignKey(Studio, on_delete=models.PROTECT, null=True, blank=True, verbose_name='Студия')
    booking_kind = models.CharField('Тип записи', max_length=10, choices=StudioBooking.Kind.choices, default='audio')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='proposals')
    status = models.CharField(max_length=12, choices=Status.choices, default='pending')
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    event = models.OneToOneField(Event, on_delete=models.PROTECT, null=True, blank=True)
    booking = models.OneToOneField(StudioBooking, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.CheckConstraint(condition=Q(ends_at__gt=F('starts_at')), name='proposal_positive_duration')]

    def clean(self):
        super().clean()
        if self.kind == 'booking' and not self.studio_id:
            raise ValidationError({'studio': 'Выберите студию для предложения брони.'})

    def get_absolute_url(self):
        return reverse('newsroom:proposal_detail', args=[self.pk])


class AuditEntry(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    object_type = models.CharField(max_length=50)
    object_id = models.PositiveBigIntegerField()
    action = models.CharField(max_length=250)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
