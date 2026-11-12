"""An engineer claims their own video work once, without overlapping assignments."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as datetime_timezone
from pathlib import Path
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import connections
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import AuditEntry, Department, EmployeeProfile, Notification, Studio, StudioBooking
from .permissions import assign_role, setup_roles
from .services import claim_booking, complete_booking_montage, delete_booking, save_booking

User = get_user_model()


@override_settings(TIME_ZONE='Asia/Novosibirsk')
class BookingEngineerClaimTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        cls.creator = User.objects.create_user(username='claim_creator', first_name='Создатель')
        assign_role(cls.creator, 'radio_reporter', Department.objects.get(slug='radio'))
        cls.chief = User.objects.create_user(username='claim_chief')
        assign_role(cls.chief, 'radio_chief', Department.objects.get(slug='radio'))
        cls.director = User.objects.create_user(username='claim_director')
        assign_role(cls.director, 'director', Department.objects.get(slug='management'))
        cls.engineer = User.objects.create_user(username='claim_engineer', first_name='Инженер')
        cls.other_engineer = User.objects.create_user(username='claim_other_engineer')
        for user in [cls.engineer, cls.other_engineer]:
            assign_role(user, 'engineer', Department.objects.get(slug='tv'))
        cls.studio = Studio.objects.create(name='Студия назначения инженера')
        cls.other_studio = Studio.objects.create(name='Другая студия назначения')

    def setUp(self):
        self.now = datetime(2027, 3, 12, 3, tzinfo=datetime_timezone.utc)
        clock = patch('django.utils.timezone.now', return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def booking(self, **kwargs):
        values = {
            'title': 'Съёмка с инженером', 'studio': self.studio, 'kind': 'video',
            'starts_at': self.now + timedelta(hours=1), 'ends_at': self.now + timedelta(hours=2),
            'created_by': self.creator, 'responsible': self.creator,
        }
        values.update(kwargs)
        return StudioBooking.objects.create(**values)

    def claim_audit(self, entry):
        return AuditEntry.objects.filter(object_type='studiobooking', object_id=entry.pk,
                                         action='Инженер принял видеосъёмку')

    def test_engineer_can_claim_future_and_current_video_without_changing_ownership(self):
        future = self.booking()
        current = self.booking(studio=self.other_studio, starts_at=self.now - timedelta(minutes=15),
                               ends_at=self.now + timedelta(minutes=30))
        for entry, status in [(future, 'planned'), (current, 'recording')]:
            with self.subTest(status=status):
                saved = claim_booking(self.engineer, entry)
                saved.refresh_from_db()
                self.assertEqual(saved.engineer, self.engineer)
                self.assertEqual(saved.created_by, self.creator)
                self.assertEqual(saved.responsible, self.creator)
                self.assertEqual(saved.status, status)
                self.assertEqual(self.claim_audit(saved).get().actor, self.engineer)
                self.assertEqual(Notification.objects.filter(url=saved.get_absolute_url(),
                                                             recipient=self.creator).count(), 1)
        self.client.force_login(self.engineer)
        response = self.client.get(reverse('newsroom:home'), follow=True)
        self.assertRedirects(response, reverse('newsroom:bookings'))
        self.assertEqual({entry.pk for entry in response.context['bookings']}, {future.pk, current.pk})
        self.assertContains(response, 'Инженер: Инженер', count=2)
        for entry in [future, current]:
            detail = self.client.get(entry.get_absolute_url())
            self.assertEqual(detail.context['booking'].engineer, self.engineer)
            self.assertContains(detail, '<span class="badge badge-green">Вы</span>', html=True)
            self.assertNotContains(detail, 'Взять бронь в работу')

    def test_own_replay_has_no_side_effects_and_another_engineer_cannot_take_stale_booking(self):
        entry = self.booking()
        stale = StudioBooking.objects.get(pk=entry.pk)
        claim_booking(self.engineer, entry)
        notifications = list(Notification.objects.filter(url=entry.get_absolute_url()).values_list('pk', flat=True))
        audit_ids = list(self.claim_audit(entry).values_list('pk', flat=True))
        replay = claim_booking(self.engineer, stale)
        self.assertEqual(replay.engineer_id, self.engineer.pk)
        self.assertEqual(list(self.claim_audit(entry).values_list('pk', flat=True)), audit_ids)
        self.assertEqual(list(Notification.objects.filter(url=entry.get_absolute_url()).values_list('pk', flat=True)), notifications)
        with self.assertRaises(ValidationError):
            claim_booking(self.other_engineer, stale)
        self.client.force_login(self.other_engineer)
        response = self.client.post(reverse('newsroom:booking_action', args=[entry.pk]), {'action': 'claim'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'message-error')
        entry.refresh_from_db()
        self.assertEqual(entry.engineer_id, self.engineer.pk)
        self.assertEqual(self.claim_audit(entry).count(), 1)
        self.assertEqual(Notification.objects.filter(url=entry.get_absolute_url()).count(), 1)

    def test_only_an_active_engineer_with_matching_group_and_profile_can_claim(self):
        inactive = User.objects.create_user(username='claim_inactive')
        mismatch = User.objects.create_user(username='claim_mismatch')
        for user in [inactive, mismatch]:
            assign_role(user, 'engineer', Department.objects.get(slug='tv'))
        # The caller can hold a stale active User instance after account revocation.
        User.objects.filter(pk=inactive.pk).update(is_active=False)
        mismatch.groups.set([Group.objects.get(name='radio_chief')])
        entry = self.booking()
        for user in [self.creator, self.chief, inactive, mismatch]:
            with self.subTest(user=user.username):
                with self.assertRaises(PermissionDenied):
                    claim_booking(user, entry)
                if user != inactive:
                    self.client.force_login(user)
                    self.assertEqual(self.client.post(reverse('newsroom:booking_action', args=[entry.pk]),
                                                      {'action': 'claim'}).status_code, 403)
        entry.refresh_from_db()
        self.assertIsNone(entry.engineer_id)
        self.assertFalse(self.claim_audit(entry).exists())

    def test_audio_cancelled_and_already_mounted_video_cannot_be_claimed(self):
        cases = [
            ({'kind': 'audio'}, 'planned'),
            ({'kind': 'audio', 'status': 'completed'}, 'completed'),
            ({'status': 'cancelled'}, 'cancelled'),
            ({'status': 'completed', 'montage_completed': True}, 'completed'),
        ]
        for values, status in cases:
            with self.subTest(values=values):
                entry = self.booking(**values)
                with self.assertRaises(ValidationError):
                    claim_booking(self.engineer, entry)
                entry.refresh_from_db()
                self.assertIsNone(entry.engineer_id)
                self.assertEqual(entry.status, status)
                self.assertFalse(self.claim_audit(entry).exists())

    def test_completed_and_just_ended_video_can_be_claimed_without_blocking_active_work(self):
        active = self.booking(engineer=self.engineer)
        cases = [
            {'status': 'completed'},  # Early completion, even with a future scheduled end.
            {'starts_at': self.now - timedelta(hours=1), 'ends_at': self.now},
            {'status': 'recording', 'starts_at': self.now - timedelta(hours=2),
             'ends_at': self.now - timedelta(hours=1)},
        ]
        for values in cases:
            with self.subTest(values=values):
                entry = self.booking(studio=self.other_studio, **values)
                claimed = claim_booking(self.engineer, entry)
                claimed.refresh_from_db()
                self.assertEqual(claimed.engineer_id, self.engineer.pk)
                self.assertEqual(claimed.status, 'completed')
                self.assertEqual(claimed.created_by_id, self.creator.pk)
                self.assertEqual(claimed.responsible_id, self.creator.pk)
                self.assertFalse(claimed.montage_completed)
                self.assertEqual(self.claim_audit(claimed).count(), 1)
        active.refresh_from_db()
        self.assertEqual(active.status, 'planned')
        self.assertEqual(active.engineer_id, self.engineer.pk)

    def test_recording_ending_while_waiting_for_claim_lock_becomes_completed_and_claimable(self):
        waiting = self.booking(starts_at=self.now - timedelta(minutes=15),
                               ends_at=self.now + timedelta(seconds=1))
        ticks = iter([self.now, waiting.ends_at])
        with patch('newsroom.services._booking_now',
                   side_effect=lambda explicit=None: explicit if explicit is not None else next(ticks)):
            claim_booking(self.engineer, waiting)
        waiting.refresh_from_db()
        self.assertEqual(waiting.engineer_id, self.engineer.pk)
        self.assertEqual(waiting.status, 'completed')
        self.assertEqual(self.claim_audit(waiting).count(), 1)

    def test_engineer_overlap_across_studios_is_rejected_but_adjacent_slots_are_allowed(self):
        first = self.booking()
        claim_booking(self.engineer, first)
        overlap = self.booking(studio=self.other_studio, starts_at=first.starts_at + timedelta(minutes=30),
                               ends_at=first.ends_at + timedelta(minutes=30))
        with self.assertRaises(ValidationError):
            claim_booking(self.engineer, overlap)
        overlap.refresh_from_db()
        self.assertIsNone(overlap.engineer_id)
        for start, end in [(self.now, first.starts_at), (first.ends_at, first.ends_at + timedelta(hours=1))]:
            with self.subTest(start=start):
                adjacent = self.booking(studio=self.other_studio, starts_at=start, ends_at=end)
                self.assertEqual(claim_booking(self.engineer, adjacent).engineer_id, self.engineer.pk)

    def test_finished_cancelled_audio_and_another_engineer_do_not_block_assignment(self):
        for values in [{'status': 'completed'}, {'status': 'cancelled'}, {'kind': 'audio'},
                       {'engineer': self.other_engineer}]:
            with self.subTest(values=values):
                existing = self.booking(studio=self.other_studio, **{'engineer': self.engineer, **values})
                candidate = self.booking()
                self.assertEqual(claim_booking(self.engineer, candidate).engineer_id, self.engineer.pk)
                StudioBooking.objects.filter(pk__in=[existing.pk, candidate.pk]).delete()

    def test_manager_cannot_move_assigned_booking_into_overlap_or_forge_its_engineer(self):
        first = self.booking()
        second = self.booking(studio=self.other_studio, starts_at=self.now + timedelta(hours=3),
                              ends_at=self.now + timedelta(hours=4))
        for entry in [first, second]:
            claim_booking(self.engineer, entry)
        original = (second.starts_at, second.ends_at)
        second.starts_at = first.starts_at + timedelta(minutes=30)
        second.ends_at = first.ends_at + timedelta(minutes=30)
        second.engineer = self.other_engineer
        with self.assertRaises(ValidationError):
            save_booking(self.chief, second)
        second.refresh_from_db()
        self.assertEqual((second.starts_at, second.ends_at), original)
        self.assertEqual(second.engineer_id, self.engineer.pk)
        second.starts_at = first.ends_at
        second.ends_at = first.ends_at + timedelta(hours=1)
        second.engineer = None
        saved = save_booking(self.chief, second)
        saved.refresh_from_db()
        self.assertEqual(saved.engineer_id, self.engineer.pk)
        self.assertEqual(saved.created_by, self.creator)
        self.assertEqual(saved.responsible, self.creator)

    def test_http_claim_is_post_only_csrf_protected_and_deleted_booking_returns_404(self):
        entry = self.booking()
        action_url = reverse('newsroom:booking_action', args=[entry.pk])
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.engineer)
        detail = client.get(entry.get_absolute_url())
        self.assertContains(detail, 'Взять бронь в работу')
        self.assertEqual(client.get(action_url).status_code, 405)
        self.assertEqual(client.post(action_url, {'action': 'claim'}).status_code, 403)
        response = client.post(action_url, {'action': 'claim', 'engineer': self.other_engineer.pk,
                                           'csrfmiddlewaretoken': client.cookies['csrftoken'].value}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Взять бронь в работу')
        entry.refresh_from_db()
        self.assertEqual(entry.engineer_id, self.engineer.pk)
        statuses = client.get(reverse('newsroom:booking_statuses'), {'ids': entry.pk}).json()['bookings']
        self.assertEqual(statuses[0]['engineer_id'], self.engineer.pk)
        schedule = client.get(reverse('newsroom:booking_day_schedule'), {
            'date': timezone.localtime(entry.starts_at).date().isoformat(), 'studio': self.studio.pk,
        }).json()['bookings']
        self.assertEqual(schedule[0]['engineer_id'], self.engineer.pk)
        delete_booking(self.chief, entry)
        self.assertEqual(client.post(action_url, {'action': 'claim',
                                                  'csrfmiddlewaretoken': client.cookies['csrftoken'].value}).status_code, 404)

    def test_http_completed_claim_gives_montage_exclusively_to_the_claiming_engineer(self):
        entry = self.booking(status='completed', starts_at=self.now - timedelta(hours=2),
                             ends_at=self.now - timedelta(hours=1))
        action_url = reverse('newsroom:booking_action', args=[entry.pk])
        self.client.force_login(self.engineer)
        detail = self.client.get(entry.get_absolute_url())
        self.assertContains(detail, 'Взять бронь в работу')
        self.assertNotContains(detail, 'Отметить «Смонтировано»')
        response = self.client.post(action_url, {'action': 'claim', 'engineer': self.other_engineer.pk}, follow=True)
        self.assertRedirects(response, entry.get_absolute_url())
        self.assertContains(response, 'Отметить «Смонтировано»')
        self.assertNotContains(response, 'Взять бронь в работу')
        entry.refresh_from_db()
        self.assertEqual(entry.engineer_id, self.engineer.pk, 'the POST cannot choose another engineer')
        self.assertEqual(entry.status, 'completed')
        self.assertFalse(entry.montage_completed)

        for actor in [self.other_engineer, self.director, self.creator]:
            with self.subTest(actor=actor.username):
                self.client.force_login(actor)
                detail = self.client.get(entry.get_absolute_url())
                self.assertNotContains(detail, 'Отметить «Смонтировано»')
                self.assertNotContains(detail, 'Взять бронь в работу')
                self.assertEqual(self.client.post(action_url, {'action': 'montage'}).status_code, 403)
                with self.assertRaises(PermissionDenied):
                    complete_booking_montage(actor, entry)
        entry.refresh_from_db()
        self.assertFalse(entry.montage_completed)

        claim_notice_ids = list(Notification.objects.filter(url=entry.get_absolute_url()).values_list('pk', flat=True))
        self.client.force_login(self.engineer)
        self.assertRedirects(self.client.post(action_url, {'action': 'claim'}), entry.get_absolute_url())
        self.assertEqual(self.claim_audit(entry).count(), 1)
        self.assertEqual(list(Notification.objects.filter(url=entry.get_absolute_url()).values_list('pk', flat=True)),
                         claim_notice_ids)
        response = self.client.post(action_url, {'action': 'montage'}, follow=True)
        self.assertRedirects(response, entry.get_absolute_url())
        self.assertNotContains(response, 'Отметить «Смонтировано»')
        entry.refresh_from_db()
        self.assertEqual(entry.status, 'completed')
        self.assertTrue(entry.montage_completed)
        completed_audit = AuditEntry.objects.filter(object_type='studiobooking', object_id=entry.pk,
                                                    action='Монтаж студийной видеозаписи завершён')
        self.assertEqual(completed_audit.get().actor, self.engineer)
        completed_notice = Notification.objects.filter(url=entry.get_absolute_url(), recipient=self.creator,
                                                       title='Студийная видеозапись смонтирована')
        self.assertEqual(completed_notice.count(), 1)
        response = self.client.post(action_url, {'action': 'montage'}, follow=True)
        self.assertContains(response, 'message-error')
        self.assertEqual(completed_audit.count(), 1)
        self.assertEqual(completed_notice.count(), 1)
        self.assertRedirects(self.client.post(action_url, {'action': 'claim'}), entry.get_absolute_url())
        self.assertEqual(self.claim_audit(entry).count(), 1)

    def test_assigned_engineer_cannot_mark_montage_for_unfinished_audio_or_cancelled_work(self):
        cases = [
            {},
            {'starts_at': self.now - timedelta(minutes=15), 'ends_at': self.now + timedelta(minutes=30)},
            {'kind': 'audio', 'status': 'completed'},
            {'status': 'cancelled'},
        ]
        self.client.force_login(self.engineer)
        for values in cases:
            with self.subTest(values=values):
                entry = self.booking(engineer=self.engineer, **values)
                with self.assertRaises(ValidationError):
                    complete_booking_montage(self.engineer, entry)
                self.assertNotContains(self.client.get(entry.get_absolute_url()), 'Отметить «Смонтировано»')
                entry.refresh_from_db()
                self.assertFalse(entry.montage_completed)

    def test_montage_rechecks_revoked_engineer_accounts_even_with_stale_user_instances(self):
        for revoked in ['inactive', 'mismatched_profile']:
            with self.subTest(revoked=revoked):
                actor = User.objects.select_related('employee').get(pk=self.engineer.pk)
                self.assertEqual(actor.employee.role, 'engineer')
                entry = self.booking(status='completed', engineer=actor)
                if revoked == 'inactive':
                    User.objects.filter(pk=actor.pk).update(is_active=False)
                else:
                    EmployeeProfile.objects.filter(user=actor).update(role='tv_reporter')
                with self.assertRaises(PermissionDenied):
                    complete_booking_montage(actor, entry)
                entry.refresh_from_db()
                self.assertFalse(entry.montage_completed)
                self.assertFalse(AuditEntry.objects.filter(object_type='studiobooking', object_id=entry.pk,
                                                          action='Монтаж студийной видеозаписи завершён').exists())
                User.objects.filter(pk=actor.pk).update(is_active=True)
                EmployeeProfile.objects.filter(user=actor).update(role='engineer')

    def test_new_video_work_notifications_skip_archives_and_include_active_audio_to_video_changes(self):
        mismatched = User.objects.create_user(username='notice_mismatched_engineer')
        assign_role(mismatched, 'engineer', Department.objects.get(slug='tv'))
        mismatched.groups.add(Group.objects.get(name='radio_chief'))

        def new_work(entry):
            return Notification.objects.filter(url=entry.get_absolute_url(), title__startswith='Новая видеосъёмка')

        archived = save_booking(self.creator, StudioBooking(
            title='Архив с видео', studio=self.studio, kind='video',
            starts_at=self.now - timedelta(hours=2), ends_at=self.now - timedelta(hours=1),
        ))
        self.assertEqual(archived.status, 'completed')
        self.assertFalse(new_work(archived).exists())
        active_audio = save_booking(self.creator, StudioBooking(
            title='Добавлено видео', studio=self.studio, kind='audio',
            starts_at=self.now + timedelta(hours=3), ends_at=self.now + timedelta(hours=4),
        ))
        self.assertFalse(new_work(active_audio).exists())
        active_audio.kind = 'video'
        active_audio = save_booking(self.chief, active_audio)
        self.assertEqual(set(new_work(active_audio).values_list('recipient_id', flat=True)),
                         {self.engineer.pk, self.other_engineer.pk})
        old_audio = self.booking(kind='audio', status='completed', starts_at=self.now - timedelta(hours=4),
                                 ends_at=self.now - timedelta(hours=3))
        old_audio.kind = 'video'
        old_audio = save_booking(self.chief, old_audio)
        self.assertFalse(new_work(old_audio).exists())

    def test_assigned_engineer_is_notified_of_slot_changes_including_a_move_to_the_past(self):
        entry = self.booking()
        claim_booking(self.engineer, entry)
        entry.refresh_from_db()
        entry.title = 'Уточнённое название'
        entry.description = 'Только описание'
        entry = save_booking(self.chief, entry)
        self.assertFalse(Notification.objects.filter(recipient=self.engineer,
                                                     url=entry.get_absolute_url()).exists())
        entry.studio = self.other_studio
        entry.starts_at += timedelta(days=1)
        entry.ends_at += timedelta(days=1)
        entry = save_booking(self.chief, entry)
        notices = Notification.objects.filter(recipient=self.engineer, url=entry.get_absolute_url(),
                                               title__startswith='Изменено время')
        notice = notices.get()
        for value in [entry.title, self.other_studio.name,
                      timezone.localtime(entry.starts_at).strftime('%d.%m.%Y %H:%M'),
                      timezone.localtime(entry.ends_at).strftime('%H:%M')]:
            self.assertIn(value, notice.text)
        entry.starts_at = self.now - timedelta(hours=2)
        entry.ends_at = self.now - timedelta(hours=1)
        entry = save_booking(self.chief, entry)
        entry.refresh_from_db()
        self.assertEqual(entry.status, 'completed')
        self.assertEqual(entry.engineer_id, self.engineer.pk)
        self.assertEqual(notices.count(), 2)
        self.assertFalse(Notification.objects.filter(url=entry.get_absolute_url(),
                                                     title__startswith='Новая видеосъёмка').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.engineer, url=entry.get_absolute_url(),
                                                    title__contains='запись завершена').exists())

    def test_deleted_assigned_booking_notifies_engineer_with_a_working_calendar_link(self):
        entry = self.booking()
        claim_booking(self.engineer, entry)
        booking_url = entry.get_absolute_url()
        day = timezone.localtime(entry.starts_at).date().isoformat()
        delete_booking(self.chief, entry)
        self.assertFalse(Notification.objects.filter(url=booking_url).exists())
        notice = Notification.objects.get(recipient=self.engineer, title__contains='удалена')
        self.assertIn(entry.title, notice.text)
        self.assertEqual(urlsplit(notice.url).path, reverse('newsroom:bookings'))
        self.assertEqual(parse_qs(urlsplit(notice.url).query), {'date': [day], 'view': ['week']})
        self.client.force_login(self.engineer)
        self.assertEqual(self.client.get(notice.url).status_code, 200)

    def test_concurrent_claims_have_one_winner_on_a_real_sqlite_file(self):
        if connections['default'].vendor != 'sqlite':
            self.skipTest('SQLite transaction serialization is the deployed database contract.')
        # A separate process makes every service connection use this disposable file.
        with TemporaryDirectory(prefix='volna-engineer-race-') as directory:
            database = str(Path(directory) / 'claims.sqlite3')
            environment = {**os.environ, 'DATABASE_PATH': database, 'DJANGO_SETTINGS_MODULE': 'config.settings'}
            result = subprocess.run([
                sys.executable, '-c',
                'import sys, django; django.setup(); '
                'from newsroom.test_booking_engineer import _sqlite_claim_races; '
                '_sqlite_claim_races(sys.argv[1])', database,
            ], cwd=settings.BASE_DIR, env=environment, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('All real SQLite claim races passed.', result.stdout)


def _sqlite_claim_races(expected_database):
    """Run in a subprocess with an explicit disposable database, never the working DB."""
    if str(settings.DATABASES['default']['NAME']) != expected_database:
        raise RuntimeError('The concurrency check requires its own database file.')
    call_command('migrate', verbosity=0, interactive=False)
    setup_roles()
    creator = User.objects.create_user(username='race_creator')
    assign_role(creator, 'radio_reporter', Department.objects.get(slug='radio'))
    engineers = [User.objects.create_user(username=f'race_engineer_{index}') for index in range(2)]
    for engineer in engineers:
        assign_role(engineer, 'engineer', Department.objects.get(slug='tv'))
    studios = [Studio.objects.create(name=f'Race studio {index}') for index in range(2)]
    now = datetime(2027, 3, 12, 3, tzinfo=datetime_timezone.utc)

    def booking(studio, hour, status='planned'):
        return StudioBooking.objects.create(title='Concurrent video', studio=studio, kind='video',
                                            starts_at=now + timedelta(hours=hour),
                                            ends_at=now + timedelta(hours=hour + 1),
                                            created_by=creator, responsible=creator, status=status)

    def race(pairs):
        barrier = Barrier(2)

        def attempt(user_id, booking_id):
            try:
                user = User.objects.get(pk=user_id)
                stale = StudioBooking.objects.get(pk=booking_id)
                barrier.wait(timeout=5)
                try:
                    claim_booking(user, stale)
                    return 'claimed'
                except ValidationError:
                    return 'rejected'
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(attempt, user_id, booking_id) for user_id, booking_id in pairs]
            outcomes = [future.result(timeout=25) for future in futures]
        assert sorted(outcomes) == ['claimed', 'rejected'], outcomes

    with patch('django.utils.timezone.now', return_value=now):
        shared = booking(studios[0], 1)
        race([(engineer.pk, shared.pk) for engineer in engineers])
        shared.refresh_from_db()
        assert shared.engineer_id in [engineer.pk for engineer in engineers]
        assert AuditEntry.objects.filter(object_type='studiobooking', object_id=shared.pk,
                                         action='Инженер принял видеосъёмку').count() == 1
        assert Notification.objects.filter(url=shared.get_absolute_url()).count() == 1
        parallel = [booking(studio, 3) for studio in studios]
        race([(engineers[0].pk, entry.pk) for entry in parallel])
        assert StudioBooking.objects.filter(pk__in=[entry.pk for entry in parallel],
                                            engineer=engineers[0]).count() == 1
        assert AuditEntry.objects.filter(object_type='studiobooking', object_id__in=[entry.pk for entry in parallel],
                                         action='Инженер принял видеосъёмку').count() == 1
        assert Notification.objects.filter(url__in=[entry.get_absolute_url() for entry in parallel]).count() == 1
        completed = booking(studios[0], -3, status='completed')
        race([(engineer.pk, completed.pk) for engineer in engineers])
        completed.refresh_from_db()
        assert completed.status == 'completed'
        assert completed.engineer_id in [engineer.pk for engineer in engineers]
        assert AuditEntry.objects.filter(object_type='studiobooking', object_id=completed.pk,
                                         action='Инженер принял видеосъёмку').count() == 1
        assert Notification.objects.filter(url=completed.get_absolute_url()).count() == 1
    connections.close_all()
    print('All real SQLite claim races passed.')
