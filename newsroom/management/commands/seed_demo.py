import secrets
from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from newsroom.models import (
    BroadcastShift, Department, Event, EventAssignment, EventStatusHistory,
    Notification, Proposal, Role, Studio, StudioBooking, UploadedFile,
)
from newsroom.permissions import assign_role, role_of, setup_roles


NAMES = {
    'director': 'Анна Волкова', 'radio_chief': 'Михаил Соколов',
    'radio_reporter': 'Алина Морозова', 'engineer': 'Павел Крылов',
    'tv_chief': 'Ольга Лебедева', 'tv_reporter': 'Мария Белова',
    'operator': 'Дмитрий Орлов', 'photographer': 'Екатерина Зайцева',
    'editor': 'Артём Мельников', 'site_chief': 'Наталья Смирнова',
    'site_editor': 'Илья Кузнецов', 'producer': 'Виктория Иванова',
}


def moment(day, hour, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


class Command(BaseCommand):
    help = 'Создать помеченные демонстрационные данные и случайные пароли без дубликатов.'

    @transaction.atomic
    def handle(self, *args, **options):
        setup_roles()
        departments = {}
        for slug, name in [('management', 'Руководство'), ('radio', 'Радио'), ('tv', 'Телевидение'), ('site', 'Новостной сайт'), ('production', 'Продюсерский отдел')]:
            departments[slug], _ = Department.objects.get_or_create(slug=slug, defaults={'name': name})
        accounts = {}
        created_passwords = []
        User = get_user_model()
        for role, label in Role.choices:
            username = f'demo_{role}'
            user, created = User.objects.get_or_create(username=username, defaults={'first_name': NAMES[role]})
            if created:
                password = secrets.token_urlsafe(15)
                user.set_password(password)
                user.save(update_fields=['password'])
                created_passwords.append((username, password, label))
                department = 'management' if role == 'director' else 'radio' if role.startswith('radio_') else 'site' if role.startswith('site_') else 'production' if role == 'producer' else 'tv'
                assign_role(user, role, departments[department])
            elif role_of(user) != role:
                raise CommandError(f'Логин {username} занят аккаунтом без ожидаемой демонстрационной роли. Данные не изменены.')
            accounts[role] = user
        studio1, _ = Studio.objects.get_or_create(name='Новая студия')
        studio2, _ = Studio.objects.get_or_create(name='Старая студия')
        today = timezone.localdate()
        monday = today - timedelta(days=today.weekday())
        for index in range(14):
            day = monday + timedelta(days=index)
            BroadcastShift.objects.get_or_create(demo_key=f'demo-shift-{index}', defaults={
                'employee': accounts['radio_reporter'], 'starts_at': moment(day, 8),
                'ends_at': moment(day, 11), 'created_by': accounts['radio_chief'],
            })
        bookings = [
            ('morning', 'ДЕМО · Утренний эфир', studio1, today, 9, 11, 'audio', 'planned'),
            ('interview', 'ДЕМО · Интервью с гостем', studio2, today, 13, 15, 'video', 'planned'),
            ('podcast', 'ДЕМО · Подкаст о городе', studio1, today + timedelta(days=1), 12, 14, 'audio', 'planned'),
            ('finished', 'ДЕМО · Запись выпуска', studio2, today - timedelta(days=1), 10, 12, 'video', 'completed'),
        ]
        for key, title, studio, day, start, end, kind, status in bookings:
            StudioBooking.objects.get_or_create(demo_key=f'demo-booking-{key}', defaults={
                'title': title, 'description': 'Демонстрационная запись для знакомства с системой.',
                'studio': studio, 'starts_at': moment(day, start), 'ends_at': moment(day, end),
                'kind': kind, 'status': status, 'responsible': accounts['radio_reporter'],
                'created_by': accounts['radio_chief'], 'engineer': accounts['engineer'] if key == 'finished' else None,
                'montage_completed': key == 'finished',
            })
        event_titles = [
            ('planned', 'ДЕМО · Открытие новой набережной', 1, 'Городская набережная'),
            ('shot', 'ДЕМО · Интервью в музее', 0, 'Музей города'),
            ('transferred', 'ДЕМО · Городские инициативы', -1, 'Пресс-центр'),
            ('edited', 'ДЕМО · Люди нашего города', -2, 'Центральная площадь'),
            ('approved', 'ДЕМО · Новый культурный сезон', -3, 'Дом культуры'),
            ('published', 'ДЕМО · Спортивные выходные', -4, 'Городской стадион'),
        ]
        statuses = list(Event.Status.values)
        for status, title, offset, location in event_titles:
            day = today + timedelta(days=offset)
            event, created = Event.objects.get_or_create(demo_key=f'demo-event-{status}', defaults={
                'title': title, 'description': 'Демонстрационный инфоповод. Проверьте назначения, файлы и историю этапов.',
                'location': location, 'starts_at': moment(day, 11), 'ends_at': moment(day, 13),
                'created_by': accounts['tv_chief'], 'reporter': accounts['tv_reporter'],
                'camera': accounts['operator'], 'photographer': accounts['photographer'],
                'editor': accounts['editor'], 'status': status,
                'shooting_completed_at': moment(day, 13) if status != 'planned' else None,
            })
            if not created:
                continue
            EventAssignment.objects.create(event=event, employee=accounts['operator'], job='camera', status='assigned' if status == 'planned' else 'completed')
            EventAssignment.objects.create(event=event, employee=accounts['photographer'], job='photographer', status='assigned' if status == 'planned' else 'completed')
            previous = ''
            for stage in statuses[:statuses.index(status) + 1]:
                actor_role = 'tv_chief' if stage in {'planned', 'approved'} else 'editor' if stage == 'edited' else 'site_editor' if stage == 'published' else 'tv_reporter'
                EventStatusHistory.objects.create(event=event, actor=accounts[actor_role], old_status=previous, new_status=stage)
                previous = stage
            if status != 'planned':
                upload = UploadedFile(title='ДЕМО · Редакционная справка', original_name='demo-brief.txt', size=0, uploaded_by=accounts['tv_reporter'], event=event)
                content = 'Демонстрационный материал редакции «Волна».\n'.encode('utf-8')
                upload.size = len(content)
                upload.file.save('demo-brief.txt', ContentFile(content), save=True)
            if status == 'planned':
                for role in ['tv_reporter', 'operator', 'photographer', 'editor']:
                    Notification.objects.create(recipient=accounts[role], title='Вы назначены на инфоповод', text=title, url=event.get_absolute_url())
            elif status == 'transferred':
                Notification.objects.create(recipient=accounts['editor'], title='Материалы переданы на монтаж', text=title, url=event.get_absolute_url())
            elif status == 'edited':
                Notification.objects.create(recipient=accounts['tv_chief'], title='Видео смонтировано', text=title, url=event.get_absolute_url())
            elif status == 'approved':
                for role in ['site_chief', 'site_editor']:
                    Notification.objects.create(recipient=accounts[role], title='Материал одобрен для сайта', text=title, url=event.get_absolute_url())
        for kind, title, hour in [('event', 'ДЕМО · Истории молодых учёных', 14), ('booking', 'ДЕМО · Запись нового подкаста', 16)]:
            Proposal.objects.get_or_create(demo_key=f'demo-proposal-{kind}', defaults={
                'kind': kind, 'title': title, 'description': 'Демонстрационное предложение продюсера. Можно одобрить или отклонить.',
                'starts_at': moment(today + timedelta(days=2), hour), 'ends_at': moment(today + timedelta(days=2), hour + 1),
                'studio': studio1 if kind == 'booking' else None, 'author': accounts['producer'],
            })
        self.stdout.write(self.style.SUCCESS('Демонстрационные данные готовы. Существующие записи не изменены.'))
        for username, password, label in created_passwords:
            self.stdout.write(f'{label}: {username} / {password}')
        if created_passwords:
            self.stdout.write('Сохраните пароли: они выводятся только при создании и не хранятся открытым текстом.')
        else:
            self.stdout.write('Новых аккаунтов нет; пароли существующих пользователей не изменены.')
