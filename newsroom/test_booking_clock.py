"""Clock-driven booking transitions preserve manual decisions, access and audit history."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import AuditEntry, Department, Notification, Studio, StudioBooking
from .permissions import assign_role, setup_roles
from .services import (
    claim_booking, complete_booking_montage, save_booking,
    set_booking_status, sync_booking_statuses,
)

User = get_user_model()


@override_settings(TIME_ZONE='Asia/Novosibirsk')
class BookingClockTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        cls.users = {}
        for role, department in [('director', 'management'), ('radio_chief', 'radio'),
                                 ('radio_reporter', 'radio'), ('engineer', 'tv'), ('operator', 'tv')]:
            user = User.objects.create_user(username=f'clock_{role}')
            assign_role(user, role, Department.objects.get(slug=department))
            cls.users[role] = user
        cls.studio = Studio.objects.create(name='Студия с автоматическим статусом')

    def setUp(self):
        self.now = datetime(2027, 3, 12, 3, tzinfo=datetime_timezone.utc)  # 10:00 in the newsroom.
        self.clock = patch('django.utils.timezone.now', return_value=self.now)
        self.mock_now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.reporter = self.users['radio_reporter']
        self.client.force_login(self.reporter)

    def booking(self, **kwargs):
        values = {
            'title': 'Запись по расписанию', 'studio': self.studio,
            'starts_at': self.now + timedelta(hours=1), 'ends_at': self.now + timedelta(hours=2),
            'responsible': self.reporter, 'created_by': self.reporter,
        }
        values.update(kwargs)
        return StudioBooking.objects.create(**values)

    def state(self, booking):
        booking.refresh_from_db()
        return booking.status

    def system_audit(self, booking):
        return AuditEntry.objects.filter(object_type='studiobooking', object_id=booking.pk,
                                         action__startswith='Автоматически:')

    def test_exact_microsecond_boundaries_use_real_instants_across_local_and_utc_dates(self):
        local = ZoneInfo('Asia/Novosibirsk')
        start = datetime(2027, 3, 12, 6, 30, tzinfo=local)
        end = datetime(2027, 3, 12, 8, tzinfo=local)
        entry = self.booking(starts_at=start, ends_at=end)
        self.assertNotEqual(start.astimezone(datetime_timezone.utc).date(), end.astimezone(datetime_timezone.utc).date())
        for tick, expected, changes in [
            (start - timedelta(microseconds=1), 'planned', 0),
            (start, 'recording', 1),
            (end - timedelta(microseconds=1), 'recording', 0),
            (end, 'completed', 1),
            (end + timedelta(microseconds=1), 'completed', 0),
        ]:
            with self.subTest(tick=tick):
                self.assertEqual(sync_booking_statuses(now=tick, booking_ids=[entry.pk]), changes)
                self.assertEqual(self.state(entry), expected)

    def test_missed_start_catches_up_directly_to_completed_without_an_open_page(self):
        entries = [self.booking(status=status, starts_at=self.now - timedelta(hours=2), ends_at=self.now - timedelta(hours=1))
                   for status in ['planned', 'recording']]
        self.assertEqual(sync_booking_statuses(), 2)
        for entry in entries:
            self.assertEqual(self.state(entry), 'completed')
            self.assertEqual(self.system_audit(entry).count(), 1)

    def test_repeated_ticks_do_not_duplicate_completion_notifications_or_system_audit(self):
        entry = self.booking(starts_at=self.now - timedelta(hours=2), ends_at=self.now - timedelta(hours=1))
        self.assertEqual(sync_booking_statuses(), 1)
        notifications = set(Notification.objects.filter(url=entry.get_absolute_url()).values_list('pk', flat=True))
        self.assertTrue(notifications)
        audit = self.system_audit(entry).get()
        self.assertIsNone(audit.actor_id)
        self.assertEqual(sync_booking_statuses(now=self.now + timedelta(hours=1)), 0)
        self.assertEqual(self.system_audit(entry).count(), 1)
        self.assertEqual(set(Notification.objects.filter(url=entry.get_absolute_url()).values_list('pk', flat=True)), notifications)

    def test_manual_early_completion_and_legacy_cancelled_rows_never_reopen(self):
        entry = self.booking(starts_at=self.now - timedelta(minutes=30), ends_at=self.now + timedelta(minutes=30))
        set_booking_status(self.reporter, entry, 'completed')
        cancelled = self.booking(status='cancelled', starts_at=entry.starts_at, ends_at=entry.ends_at)
        replacement = StudioBooking(title='Освободившийся слот', studio=self.studio, responsible=self.reporter,
                                    starts_at=entry.starts_at, ends_at=entry.ends_at)
        saved = save_booking(self.reporter, replacement)
        self.assertEqual(saved.status, 'recording')
        sync_booking_statuses(now=self.now + timedelta(minutes=10))
        self.assertEqual(self.state(entry), 'completed')
        self.assertEqual(self.state(cancelled), 'cancelled')
        sync_booking_statuses(now=self.now + timedelta(hours=1))
        self.assertEqual(self.state(entry), 'completed')
        self.assertEqual(self.state(cancelled), 'cancelled')

    def test_save_derives_current_and_past_states_and_rescheduling_active_work_to_future_resets_it(self):
        current = StudioBooking(title='Текущая запись', studio=self.studio, responsible=self.reporter,
                                starts_at=self.now - timedelta(minutes=30), ends_at=self.now + timedelta(minutes=30))
        current = save_booking(self.reporter, current)
        self.assertEqual(current.status, 'recording')
        past = StudioBooking(title='Старая запись', studio=self.studio, responsible=self.reporter,
                             starts_at=self.now - timedelta(hours=2), ends_at=self.now - timedelta(hours=1))
        self.assertEqual(save_booking(self.reporter, past).status, 'completed')
        current.starts_at = self.now + timedelta(hours=2)
        current.ends_at = self.now + timedelta(hours=3)
        current = save_booking(self.users['radio_chief'], current)
        self.assertEqual(current.status, 'planned')

    def test_manual_start_is_rejected_in_service_and_forged_http_post(self):
        entry = self.booking()
        with self.assertRaises(ValidationError):
            set_booking_status(self.reporter, entry, 'recording')
        self.assertEqual(self.state(entry), 'planned')
        response = self.client.post(reverse('newsroom:booking_action', args=[entry.pk]),
                                    {'action': 'status', 'status': 'recording'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.state(entry), 'planned')
        self.assertFalse(any(action.get('status') == 'recording' for action in response.context['actions']))

    def test_completion_button_is_available_only_from_start_until_end(self):
        entry = self.booking()
        for tick, expected_status, available in [
            (entry.starts_at - timedelta(microseconds=1), 'planned', False),
            (entry.starts_at, 'recording', True),
            (entry.ends_at, 'completed', False),
        ]:
            with self.subTest(tick=tick):
                self.mock_now.return_value = tick
                response = self.client.get(entry.get_absolute_url())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.state(entry), expected_status)
                self.assertEqual(any(action.get('status') == 'completed'
                                     for action in response.context['actions']), available)
                if available:
                    self.assertContains(response, 'Завершить запись')
                else:
                    self.assertNotContains(response, 'Завершить запись')
        with self.assertRaises(ValidationError):
            set_booking_status(self.users['radio_chief'], entry, 'completed')

    def test_owner_and_manager_cannot_finish_future_recording_but_can_finish_at_start(self):
        for actor in [self.reporter, self.users['radio_chief']]:
            with self.subTest(actor=actor.username):
                self.mock_now.return_value = self.now
                # A stale status must not bypass the actual schedule boundary.
                entry = self.booking(status='recording')
                with self.assertRaises(ValidationError):
                    set_booking_status(actor, entry, 'completed')
                self.assertEqual(self.state(entry), 'planned')
                self.client.force_login(actor)
                url = reverse('newsroom:booking_action', args=[entry.pk])
                response = self.client.post(url, {'action': 'status', 'status': 'completed'}, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'message-error')
                self.assertEqual(self.state(entry), 'planned')
                self.mock_now.return_value = entry.starts_at
                response = self.client.post(url, {'action': 'status', 'status': 'completed'}, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.state(entry), 'completed')
                self.assertEqual(AuditEntry.objects.filter(object_type='studiobooking', object_id=entry.pk,
                                                          actor=actor, action='Статус: recording → completed').count(), 1)

    def test_middleware_catches_up_for_authorized_viewers_but_not_forbidden_or_anonymous_requests(self):
        entry = self.booking(starts_at=self.now - timedelta(hours=2), ends_at=self.now - timedelta(hours=1))
        self.client.force_login(self.users['operator'])
        self.assertEqual(self.client.get(reverse('newsroom:bookings')).status_code, 403)
        self.assertEqual(self.state(entry), 'planned')
        self.client.logout()
        self.assertEqual(self.client.get(reverse('newsroom:bookings')).status_code, 302)
        self.assertEqual(self.state(entry), 'planned')
        self.client.force_login(self.users['engineer'])
        self.assertEqual(self.client.get(reverse('newsroom:bookings')).status_code, 200)
        self.assertEqual(self.state(entry), 'completed')
        self.assertEqual(self.client.get(reverse('newsroom:booking_edit', args=[entry.pk])).status_code, 403)

    def test_expired_video_cannot_be_claimed_but_assigned_engineer_can_finish_montage_after_auto_end(self):
        expired = self.booking(kind='video', starts_at=self.now - timedelta(hours=2), ends_at=self.now - timedelta(hours=1))
        with self.assertRaises(ValidationError):
            claim_booking(self.users['engineer'], expired)
        self.assertEqual(self.state(expired), 'completed')
        assigned = self.booking(kind='video', engineer=self.users['engineer'], starts_at=self.now - timedelta(hours=2), ends_at=self.now - timedelta(hours=1))
        complete_booking_montage(self.users['engineer'], assigned)
        assigned.refresh_from_db()
        self.assertEqual(assigned.status, 'completed')
        self.assertTrue(assigned.montage_completed)

    def test_management_command_synchronizes_once_and_is_idempotent(self):
        entry = self.booking(starts_at=self.now - timedelta(hours=2), ends_at=self.now - timedelta(hours=1))
        output = StringIO()
        call_command('sync_booking_statuses', stdout=output)
        self.assertEqual(self.state(entry), 'completed')
        self.assertEqual(self.system_audit(entry).count(), 1)
        call_command('sync_booking_statuses', stdout=StringIO())
        self.assertEqual(self.system_audit(entry).count(), 1)

    def test_status_endpoint_returns_server_time_confirmed_states_and_validates_access_and_ids(self):
        running = self.booking(starts_at=self.now - timedelta(minutes=1), ends_at=self.now + timedelta(minutes=1))
        future = self.booking()
        url = reverse('newsroom:booking_statuses')
        response = self.client.get(url, {'ids': f'{future.pk},{running.pk},9999999'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        data = response.json()
        self.assertEqual(datetime.fromisoformat(data['server_now']), self.now)
        self.assertEqual([row['id'] for row in data['bookings']], [running.pk, future.pk])
        self.assertEqual([row['status'] for row in data['bookings']], ['recording', 'planned'])
        self.assertEqual(self.client.get(url).json()['bookings'], [])
        for ids in ['bad', '-1', '1,,2', '1' * 19]:
            self.assertEqual(self.client.get(url, {'ids': ids}).status_code, 400)
        self.assertEqual(self.client.post(url, {'ids': str(running.pk)}).status_code, 405)
        self.client.force_login(self.users['operator'])
        self.assertEqual(self.client.get(url, {'ids': str(running.pk)}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(url, {'ids': str(running.pk)}).status_code, 302)

    def test_explicit_id_scope_only_updates_requested_bookings(self):
        first = self.booking(starts_at=self.now - timedelta(hours=2), ends_at=self.now - timedelta(hours=1))
        second = self.booking(starts_at=self.now - timedelta(hours=2), ends_at=self.now - timedelta(hours=1))
        self.assertEqual(sync_booking_statuses(booking_ids=[]), 0)
        self.assertEqual(sync_booking_statuses(booking_ids=[first.pk]), 1)
        self.assertEqual(self.state(first), 'completed')
        self.assertEqual(self.state(second), 'planned')
        self.assertEqual(sync_booking_statuses(booking_ids=[second.pk]), 1)
        self.assertEqual(sync_booking_statuses(booking_ids=[999999]), 0)

    def test_stale_future_recording_becomes_planned_and_remains_available_for_engineer_claim(self):
        entry = self.booking(status='recording', kind='video')
        self.assertEqual(sync_booking_statuses(), 1)
        self.assertEqual(self.state(entry), 'planned')
        claimed = claim_booking(self.users['engineer'], entry)
        self.assertEqual(claimed.engineer, self.users['engineer'])
        self.assertEqual(self.state(entry), 'planned')
