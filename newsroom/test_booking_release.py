"""An assigned engineer can decline unfinished work without cancelling a studio booking."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as datetime_timezone
from pathlib import Path
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import connections
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import AuditEntry, Department, EmployeeProfile, Notification, Studio, StudioBooking
from .permissions import assign_role, setup_roles
from .services import claim_booking, complete_booking_montage, release_booking, save_booking

User = get_user_model()
RELEASE_ACTION = 'Инженер отказался от видеозаписи'
AVAILABLE_NOTICE = 'Видеозапись снова доступна'
MONTAGE_ACTION = 'Монтаж студийной видеозаписи завершён'


@override_settings(TIME_ZONE='Asia/Novosibirsk')
class BookingEngineerReleaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        cls.creator = User.objects.create_user(username='release_creator', first_name='Создатель')
        assign_role(cls.creator, 'radio_reporter', Department.objects.get(slug='radio'))
        cls.director = User.objects.create_user(username='release_director')
        assign_role(cls.director, 'director', Department.objects.get(slug='management'))
        cls.engineer = User.objects.create_user(username='release_engineer', first_name='Первый инженер')
        cls.other_engineer = User.objects.create_user(username='release_other_engineer', first_name='Второй инженер')
        for user in [cls.engineer, cls.other_engineer]:
            assign_role(user, 'engineer', Department.objects.get(slug='tv'))
        cls.studio = Studio.objects.create(name='Студия отказа инженера')
        cls.other_studio = Studio.objects.create(name='Другая студия отказа')

    def setUp(self):
        self.now = datetime(2027, 3, 12, 3, tzinfo=datetime_timezone.utc)
        clock = patch('django.utils.timezone.now', return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def booking(self, **kwargs):
        values = {
            'title': 'Запись с назначенным инженером', 'description': 'Сохранить описание',
            'studio': self.studio, 'kind': 'video', 'engineer': self.engineer,
            'starts_at': self.now + timedelta(hours=1), 'ends_at': self.now + timedelta(hours=2),
            'created_by': self.creator, 'responsible': self.creator,
        }
        values.update(kwargs)
        return StudioBooking.objects.create(**values)

    def release_audit(self, entry):
        return AuditEntry.objects.filter(object_type='studiobooking', object_id=entry.pk, action=RELEASE_ACTION)

    def protected_fields(self, entry):
        return tuple(getattr(entry, field) for field in (
            'title', 'description', 'studio_id', 'kind', 'starts_at', 'ends_at',
            'created_by_id', 'responsible_id', 'status', 'montage_completed',
        ))

    def test_owner_can_release_planned_recording_and_completed_without_cancelling_booking(self):
        cases = [
            {},
            {'status': 'recording', 'starts_at': self.now - timedelta(minutes=15),
             'ends_at': self.now + timedelta(minutes=45)},
            {'status': 'completed', 'starts_at': self.now - timedelta(hours=2),
             'ends_at': self.now - timedelta(hours=1)},
        ]
        for values in cases:
            with self.subTest(values=values):
                entry = self.booking(**values)
                original = self.protected_fields(entry)
                released = release_booking(self.engineer, entry)
                released.refresh_from_db()
                self.assertIsNone(released.engineer_id)
                self.assertEqual(self.protected_fields(released), original)
                self.assertEqual(self.release_audit(released).get().actor, self.engineer)

    def test_release_notifies_creator_and_only_other_current_active_engineers_once(self):
        inactive = User.objects.create_user(username='release_inactive')
        mismatch = User.objects.create_user(username='release_mismatch')
        for user in [inactive, mismatch]:
            assign_role(user, 'engineer', Department.objects.get(slug='tv'))
        User.objects.filter(pk=inactive.pk).update(is_active=False)
        mismatch.groups.add(Group.objects.get(name='radio_chief'))
        entry = self.booking()
        stale = StudioBooking.objects.get(pk=entry.pk)
        release_booking(self.engineer, entry)
        creator_notice = Notification.objects.get(url=entry.get_absolute_url(), title=RELEASE_ACTION)
        self.assertEqual(creator_notice.recipient_id, self.creator.pk)
        self.assertIn(entry.title, creator_notice.text)
        self.assertIn(self.engineer.get_full_name(), creator_notice.text)
        available = Notification.objects.filter(url=entry.get_absolute_url(), title=AVAILABLE_NOTICE)
        self.assertEqual(set(available.values_list('recipient_id', flat=True)), {self.other_engineer.pk})
        original_notices = list(Notification.objects.filter(url=entry.get_absolute_url()).values_list('pk', flat=True))
        replay = release_booking(self.engineer, stale)
        self.assertIsNone(replay.engineer_id)
        self.assertEqual(self.release_audit(entry).count(), 1)
        self.assertEqual(list(Notification.objects.filter(url=entry.get_absolute_url()).values_list('pk', flat=True)),
                         original_notices)

    def test_another_engineer_can_take_released_work_and_a_stale_release_cannot_remove_them(self):
        entry = self.booking(status='completed')
        stale = StudioBooking.objects.get(pk=entry.pk)
        release_booking(self.engineer, entry)
        with self.assertRaises(PermissionDenied):
            complete_booking_montage(self.engineer, stale)
        claimed = claim_booking(self.other_engineer, stale)
        self.assertEqual(claimed.engineer_id, self.other_engineer.pk)
        with self.assertRaises(PermissionDenied):
            release_booking(self.engineer, stale)
        claimed.refresh_from_db()
        self.assertEqual(claimed.engineer_id, self.other_engineer.pk)
        self.assertFalse(claimed.montage_completed)
        complete_booking_montage(self.other_engineer, claimed)
        claimed.refresh_from_db()
        self.assertTrue(claimed.montage_completed)
        self.assertEqual(claimed.engineer_id, self.other_engineer.pk)

    def test_release_frees_engineer_time_but_keeps_the_studio_reserved(self):
        entry = self.booking()
        other = self.booking(studio=self.other_studio, engineer=None)
        with self.assertRaises(ValidationError):
            claim_booking(self.engineer, other)
        release_booking(self.engineer, entry)
        self.assertEqual(claim_booking(self.engineer, other).engineer_id, self.engineer.pk)
        conflicting = StudioBooking(title='Нельзя занять ту же студию', studio=self.studio, kind='audio',
                                    starts_at=entry.starts_at, ends_at=entry.ends_at)
        with self.assertRaises(ValidationError):
            save_booking(self.creator, conflicting)
        entry.refresh_from_db()
        self.assertEqual(entry.status, 'planned')
        self.assertIsNone(entry.engineer_id)

    def test_only_the_assigned_engineer_can_release_other_staff_get_403(self):
        entry = self.booking()
        url = reverse('newsroom:booking_action', args=[entry.pk])
        for actor in [self.other_engineer, self.director, self.creator]:
            with self.subTest(actor=actor.username):
                with self.assertRaises(PermissionDenied):
                    release_booking(actor, entry)
                self.client.force_login(actor)
                self.assertNotContains(self.client.get(entry.get_absolute_url()), 'Отказаться от брони')
                self.assertEqual(self.client.post(url, {'action': 'release'}).status_code, 403)
        entry.refresh_from_db()
        self.assertEqual(entry.engineer_id, self.engineer.pk)
        self.assertFalse(self.release_audit(entry).exists())

    def test_audio_cancelled_and_mounted_bookings_cannot_be_released(self):
        cases = [{'kind': 'audio'}, {'status': 'cancelled'}, {'status': 'completed', 'montage_completed': True}]
        self.client.force_login(self.engineer)
        for values in cases:
            with self.subTest(values=values):
                entry = self.booking(**values)
                with self.assertRaises(ValidationError):
                    release_booking(self.engineer, entry)
                self.assertNotContains(self.client.get(entry.get_absolute_url()), 'Отказаться от брони')
                response = self.client.post(reverse('newsroom:booking_action', args=[entry.pk]),
                                            {'action': 'release'}, follow=True)
                self.assertRedirects(response, reverse('newsroom:bookings'))
                self.assertContains(response, 'message-error')
                entry.refresh_from_db()
                self.assertEqual(entry.engineer_id, self.engineer.pk)
                self.assertFalse(self.release_audit(entry).exists())

    def test_release_rechecks_revoked_engineer_even_with_stale_user_instances(self):
        for revoked in ['inactive', 'mismatched_profile']:
            with self.subTest(revoked=revoked):
                actor = User.objects.select_related('employee').get(pk=self.engineer.pk)
                self.assertEqual(actor.employee.role, 'engineer')
                entry = self.booking()
                if revoked == 'inactive':
                    User.objects.filter(pk=actor.pk).update(is_active=False)
                else:
                    EmployeeProfile.objects.filter(user=actor).update(role='tv_reporter')
                with self.assertRaises(PermissionDenied):
                    release_booking(actor, entry)
                entry.refresh_from_db()
                self.assertEqual(entry.engineer_id, actor.pk)
                self.assertFalse(self.release_audit(entry).exists())
                User.objects.filter(pk=actor.pk).update(is_active=True)
                EmployeeProfile.objects.filter(user=actor).update(role='engineer')

    def test_release_uses_fresh_clock_if_recording_ends_while_waiting_for_lock(self):
        entry = self.booking(status='recording', starts_at=self.now - timedelta(minutes=15),
                             ends_at=self.now + timedelta(seconds=1))
        ticks = iter([self.now, entry.ends_at])
        with patch('newsroom.services._booking_now',
                   side_effect=lambda explicit=None: explicit if explicit is not None else next(ticks)):
            release_booking(self.engineer, entry)
        entry.refresh_from_db()
        self.assertEqual(entry.status, 'completed')
        self.assertIsNone(entry.engineer_id)
        self.assertFalse(entry.montage_completed)
        self.assertEqual(self.release_audit(entry).count(), 1)
        self.assertEqual(AuditEntry.objects.filter(object_type='studiobooking', object_id=entry.pk,
                                                  action__startswith='Автоматически:').count(), 1)

    def test_http_release_is_post_only_csrf_protected_returns_to_calendar_and_reopens_claim(self):
        entry = self.booking(status='completed')
        url = reverse('newsroom:booking_action', args=[entry.pk])
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.engineer)
        detail = client.get(entry.get_absolute_url())
        self.assertContains(detail, 'Отказаться от брони')
        self.assertContains(detail, 'Отметить «Смонтировано»')
        self.assertEqual(client.get(url).status_code, 405)
        self.assertEqual(client.post(url, {'action': 'release'}).status_code, 403)
        response = client.post(url, {'action': 'release', 'engineer': self.other_engineer.pk,
                                     'csrfmiddlewaretoken': client.cookies['csrftoken'].value}, follow=True)
        self.assertRedirects(response, reverse('newsroom:bookings'))
        detail = client.get(entry.get_absolute_url())
        self.assertNotContains(detail, 'Отказаться от брони')
        self.assertNotContains(detail, 'Отметить «Смонтировано»')
        self.assertContains(detail, 'Взять бронь в работу')
        entry.refresh_from_db()
        self.assertIsNone(entry.engineer_id)
        self.assertRedirects(client.post(url, {'action': 'release',
                                               'csrfmiddlewaretoken': client.cookies['csrftoken'].value}),
                             reverse('newsroom:bookings'))
        self.assertEqual(self.release_audit(entry).count(), 1)
        entry.delete()
        self.assertEqual(client.post(url, {'action': 'release',
                                           'csrfmiddlewaretoken': client.cookies['csrftoken'].value}).status_code, 404)

    def test_release_and_montage_race_has_one_winner_on_a_real_sqlite_file(self):
        if connections['default'].vendor != 'sqlite':
            self.skipTest('SQLite transaction serialization is the deployed database contract.')
        with TemporaryDirectory(prefix='volna-release-race-') as directory:
            database = str(Path(directory) / 'release.sqlite3')
            environment = {**os.environ, 'DATABASE_PATH': database, 'DJANGO_SETTINGS_MODULE': 'config.settings'}
            result = subprocess.run([
                sys.executable, '-c',
                'import sys, django; django.setup(); '
                'from newsroom.test_booking_release import _sqlite_release_montage_race; '
                '_sqlite_release_montage_race(sys.argv[1])', database,
            ], cwd=settings.BASE_DIR, env=environment, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Release/montage race passed.', result.stdout)


def _sqlite_release_montage_race(expected_database):
    """Only a disposable, explicitly supplied database is allowed in this subprocess."""
    if str(settings.DATABASES['default']['NAME']) != expected_database:
        raise RuntimeError('The concurrency check requires its own database file.')
    call_command('migrate', verbosity=0, interactive=False)
    setup_roles()
    creator = User.objects.create_user(username='release_race_creator')
    assign_role(creator, 'radio_reporter', Department.objects.get(slug='radio'))
    engineer = User.objects.create_user(username='release_race_engineer')
    other = User.objects.create_user(username='release_race_other')
    for user in [engineer, other]:
        assign_role(user, 'engineer', Department.objects.get(slug='tv'))
    studio = Studio.objects.create(name='Release race studio')
    now = datetime(2027, 3, 12, 3, tzinfo=datetime_timezone.utc)
    with patch('django.utils.timezone.now', return_value=now):
        entry = StudioBooking.objects.create(title='Release or montage', studio=studio, kind='video',
                                             starts_at=now - timedelta(hours=2), ends_at=now - timedelta(hours=1),
                                             status='completed', engineer=engineer,
                                             created_by=creator, responsible=creator)
        barrier = Barrier(2)

        def attempt(action):
            try:
                actor = User.objects.get(pk=engineer.pk)
                stale = StudioBooking.objects.get(pk=entry.pk)
                barrier.wait(timeout=5)
                try:
                    if action == 'release':
                        release_booking(actor, stale)
                    else:
                        complete_booking_montage(actor, stale)
                    return action
                except (ValidationError, PermissionDenied):
                    return 'rejected'
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(attempt, action) for action in ['release', 'montage']]
            outcomes = [future.result(timeout=25) for future in futures]
        assert outcomes.count('rejected') == 1, outcomes
        entry.refresh_from_db()
        if 'release' in outcomes:
            assert entry.engineer_id is None and not entry.montage_completed
        else:
            assert entry.engineer_id == engineer.pk and entry.montage_completed
        assert entry.status == 'completed'
        assert AuditEntry.objects.filter(object_type='studiobooking', object_id=entry.pk,
                                         action__in=[RELEASE_ACTION, MONTAGE_ACTION]).count() == 1
        assert Notification.objects.filter(url=entry.get_absolute_url(), recipient=creator,
                                            title__in=[RELEASE_ACTION, 'Студийная видеозапись смонтирована']).count() == 1
    connections.close_all()
    print('Release/montage race passed.')
