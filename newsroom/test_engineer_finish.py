"""The assigned engineer can end their running video booking without gaining editing rights."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import AuditEntry, Department, EmployeeProfile, Notification, Studio, StudioBooking
from .permissions import assign_role, can_edit_booking, setup_roles
from .services import (
    claim_booking, complete_booking_montage, delete_booking, release_booking,
    save_booking, set_booking_status,
)

User = get_user_model()


@override_settings(TIME_ZONE='Asia/Novosibirsk')
class AssignedEngineerFinishTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        cls.creator = User.objects.create_user(username='finish_creator')
        assign_role(cls.creator, 'radio_reporter', Department.objects.get(slug='radio'))
        cls.engineer = User.objects.create_user(username='finish_engineer')
        cls.other_engineer = User.objects.create_user(username='finish_other_engineer')
        for user in [cls.engineer, cls.other_engineer]:
            assign_role(user, 'engineer', Department.objects.get(slug='tv'))
        cls.studio = Studio.objects.create(name='Студия завершения инженером')

    def setUp(self):
        self.now = datetime(2027, 3, 12, 3, tzinfo=datetime_timezone.utc)
        clock = patch('django.utils.timezone.now', return_value=self.now)
        self.mock_now = clock.start()
        self.addCleanup(clock.stop)
        self.client.force_login(self.engineer)

    def booking(self, **kwargs):
        values = {
            'title': 'Текущая запись', 'description': 'Сохранить описание',
            'studio': self.studio, 'kind': 'video', 'engineer': self.engineer,
            'status': 'recording', 'starts_at': self.now - timedelta(minutes=15),
            'ends_at': self.now + timedelta(minutes=45),
            'created_by': self.creator, 'responsible': self.creator,
        }
        values.update(kwargs)
        return StudioBooking.objects.create(**values)

    def action_url(self, entry):
        return reverse('newsroom:booking_action', args=[entry.pk])

    def manual_audit(self, entry):
        return AuditEntry.objects.filter(object_type='studiobooking', object_id=entry.pk,
                                         action='Статус: recording → completed')

    def test_http_finish_preserves_booking_frees_studio_and_keeps_montage_with_its_engineer(self):
        entry = self.booking()
        protected = (entry.title, entry.description, entry.starts_at, entry.ends_at,
                     entry.studio_id, entry.created_by_id, entry.responsible_id, entry.engineer_id)
        detail = self.client.get(entry.get_absolute_url())
        self.assertContains(detail, 'Завершить запись')
        self.assertNotContains(detail, 'Изменить бронь')
        self.assertNotContains(detail, 'Удалить бронь')
        self.assertFalse(can_edit_booking(self.engineer, entry))
        self.assertEqual(self.client.post(reverse('newsroom:booking_edit', args=[entry.pk]),
                                          {'title': 'Подмена'}).status_code, 403)
        self.assertEqual(self.client.post(reverse('newsroom:booking_delete', args=[entry.pk])).status_code, 403)
        with self.assertRaises(PermissionDenied):
            save_booking(self.engineer, entry)
        with self.assertRaises(PermissionDenied):
            delete_booking(self.engineer, entry)

        response = self.client.post(self.action_url(entry), {'action': 'status', 'status': 'completed'}, follow=True)
        self.assertRedirects(response, reverse('newsroom:bookings'))
        entry.refresh_from_db()
        self.assertEqual(entry.status, 'completed')
        self.assertFalse(entry.montage_completed)
        self.assertEqual((entry.title, entry.description, entry.starts_at, entry.ends_at,
                          entry.studio_id, entry.created_by_id, entry.responsible_id, entry.engineer_id), protected)
        self.assertEqual(self.manual_audit(entry).get().actor, self.engineer)
        notices = list(Notification.objects.filter(url=entry.get_absolute_url()).values_list('pk', flat=True))
        self.assertTrue(notices)
        repeated = self.client.post(self.action_url(entry), {'action': 'status', 'status': 'completed'}, follow=True)
        self.assertRedirects(repeated, reverse('newsroom:bookings'))
        self.assertContains(repeated, 'message-error')
        self.assertEqual(self.manual_audit(entry).count(), 1)
        self.assertEqual(list(Notification.objects.filter(url=entry.get_absolute_url()).values_list('pk', flat=True)), notices)

        replacement = save_booking(self.creator, StudioBooking(
            title='Освободившийся слот', studio=self.studio,
            starts_at=entry.starts_at, ends_at=entry.ends_at,
        ))
        self.assertEqual(replacement.status, 'recording')
        detail = self.client.get(entry.get_absolute_url())
        self.assertNotContains(detail, 'Завершить запись')
        self.assertContains(detail, 'Отметить «Смонтировано»')
        with self.assertRaises(PermissionDenied):
            complete_booking_montage(self.other_engineer, entry)
        complete_booking_montage(self.engineer, entry)
        entry.refresh_from_db()
        self.assertTrue(entry.montage_completed)

    def test_unassigned_foreign_and_audio_bookings_are_not_finishable_by_the_engineer(self):
        for values in [{'engineer': None}, {'engineer': self.other_engineer}, {'kind': 'audio'}]:
            with self.subTest(values=values):
                entry = self.booking(**values)
                self.assertNotContains(self.client.get(entry.get_absolute_url()), 'Завершить запись')
                with self.assertRaises(PermissionDenied):
                    set_booking_status(self.engineer, entry, 'completed')
                self.assertEqual(self.client.post(self.action_url(entry),
                                                 {'action': 'status', 'status': 'completed'}).status_code, 403)
                entry.refresh_from_db()
                self.assertEqual(entry.status, 'recording')
                self.assertFalse(self.manual_audit(entry).exists())

    def test_button_follows_real_start_end_and_future_or_terminal_posts_cannot_finish(self):
        entry = self.booking(status='planned', starts_at=self.now + timedelta(hours=1),
                             ends_at=self.now + timedelta(hours=2))
        with self.assertRaises(ValidationError):
            set_booking_status(self.engineer, entry, 'completed')
        for tick, visible in [(entry.starts_at - timedelta(microseconds=1), False),
                              (entry.starts_at, True),
                              (entry.ends_at - timedelta(microseconds=1), True),
                              (entry.ends_at, False)]:
            with self.subTest(tick=tick):
                self.mock_now.return_value = tick
                detail = self.client.get(entry.get_absolute_url())
                self.assertEqual(any(action.get('status') == 'completed'
                                     for action in detail.context['actions']), visible)
        with self.assertRaises(ValidationError):
            set_booking_status(self.engineer, entry, 'completed')
        cancelled = self.booking(status='cancelled')
        with self.assertRaises(ValidationError):
            set_booking_status(self.engineer, cancelled, 'completed')
        self.assertNotContains(self.client.get(cancelled.get_absolute_url()), 'Завершить запись')
        self.assertFalse(self.manual_audit(entry).exists())

    def test_release_and_reassignment_revoke_the_old_engineers_finish_permission(self):
        entry = self.booking()
        stale = StudioBooking.objects.get(pk=entry.pk)
        release_booking(self.engineer, entry)
        with self.assertRaises(PermissionDenied):
            set_booking_status(self.engineer, stale, 'completed')
        claim_booking(self.other_engineer, stale)
        self.assertNotContains(self.client.get(entry.get_absolute_url()), 'Завершить запись')
        self.assertEqual(self.client.post(self.action_url(entry),
                                         {'action': 'status', 'status': 'completed'}).status_code, 403)
        self.client.force_login(self.other_engineer)
        self.assertContains(self.client.get(entry.get_absolute_url()), 'Завершить запись')
        response = self.client.post(self.action_url(entry), {'action': 'status', 'status': 'completed'}, follow=True)
        self.assertRedirects(response, reverse('newsroom:bookings'))
        self.assertEqual(self.manual_audit(entry).get().actor, self.other_engineer)

    def test_stale_engineer_account_cannot_finish_after_deactivation_or_role_mismatch(self):
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
                    set_booking_status(actor, entry, 'completed')
                entry.refresh_from_db()
                self.assertEqual(entry.status, 'recording')
                self.assertFalse(self.manual_audit(entry).exists())
                User.objects.filter(pk=actor.pk).update(is_active=True)
                EmployeeProfile.objects.filter(user=actor).update(role='engineer')

    def test_end_boundary_after_waiting_for_lock_uses_automatic_completion_only(self):
        entry = self.booking(ends_at=self.now + timedelta(seconds=1))
        ticks = iter([self.now, entry.ends_at])
        with patch('newsroom.services._booking_now',
                   side_effect=lambda explicit=None: explicit if explicit is not None else next(ticks)):
            with self.assertRaises(ValidationError):
                set_booking_status(self.engineer, entry, 'completed')
        self.assertFalse(self.manual_audit(entry).exists())
        # The normal redirect's GET confirms the clock state even when validation
        # rolled back the inner synchronization along with the rejected action.
        self.mock_now.return_value = entry.ends_at
        detail = self.client.get(entry.get_absolute_url())
        self.assertNotContains(detail, 'Завершить запись')
        entry.refresh_from_db()
        self.assertEqual(entry.status, 'completed')
        self.assertEqual(AuditEntry.objects.filter(object_type='studiobooking', object_id=entry.pk,
                                                  action__startswith='Автоматически:').count(), 1)

    def test_finish_is_post_only_csrf_protected_and_ignores_a_forged_engineer_field(self):
        entry = self.booking()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.engineer)
        client.get(entry.get_absolute_url())
        url = self.action_url(entry)
        self.assertEqual(client.get(url).status_code, 405)
        self.assertEqual(client.post(url, {'action': 'status', 'status': 'completed'}).status_code, 403)
        response = client.post(url, {'action': 'status', 'status': 'completed', 'engineer': self.other_engineer.pk,
                                     'csrfmiddlewaretoken': client.cookies['csrftoken'].value}, follow=True)
        self.assertRedirects(response, reverse('newsroom:bookings'))
        entry.refresh_from_db()
        self.assertEqual(entry.status, 'completed')
        self.assertEqual(entry.engineer_id, self.engineer.pk)
        self.assertEqual(self.manual_audit(entry).get().actor, self.engineer)
