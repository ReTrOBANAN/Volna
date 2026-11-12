"""Deleting a booking releases its slot without erasing decisions or audit history."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from io import StringIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import AuditEntry, Department, Notification, Proposal, Studio, StudioBooking
from .permissions import assign_role, setup_roles
from .services import (
    delete_booking, review_proposal, save_booking, save_proposal,
    set_booking_status, sync_booking_statuses,
)

User = get_user_model()


@override_settings(TIME_ZONE='Asia/Novosibirsk')
class BookingDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        cls.users = {}
        for role, department in [('director', 'management'), ('radio_chief', 'radio'),
                                 ('tv_chief', 'tv'), ('radio_reporter', 'radio'),
                                 ('tv_reporter', 'tv'), ('engineer', 'tv'),
                                 ('producer', 'production')]:
            user = User.objects.create_user(username=f'delete_{role}')
            assign_role(user, role, Department.objects.get(slug=department))
            cls.users[role] = user
        cls.studio = Studio.objects.create(name='Студия проверки удаления')

    def setUp(self):
        self.now = datetime(2027, 3, 12, 3, tzinfo=datetime_timezone.utc)
        clock = patch('django.utils.timezone.now', return_value=self.now)
        self.mock_now = clock.start()
        self.addCleanup(clock.stop)
        self.owner = self.users['radio_reporter']
        self.chief = self.users['radio_chief']
        self.client.force_login(self.owner)

    def booking(self, **kwargs):
        values = {
            'title': 'Удаляемая запись', 'studio': self.studio,
            'starts_at': self.now + timedelta(hours=1),
            'ends_at': self.now + timedelta(hours=2),
            'created_by': self.owner, 'responsible': self.owner,
        }
        values.update(kwargs)
        return StudioBooking.objects.create(**values)

    def delete_url(self, booking):
        return reverse('newsroom:booking_delete', args=[booking.pk])

    def test_owner_deletes_planned_and_current_bookings_releasing_slot_and_retaining_audit(self):
        for status in ['planned', 'recording']:
            with self.subTest(status=status):
                start = self.now + timedelta(hours=1) if status == 'planned' else self.now - timedelta(minutes=30)
                end = start + timedelta(hours=1)
                entry = self.booking(status=status, starts_at=start, ends_at=end)
                booking_id = entry.pk
                delete_booking(self.owner, entry)
                self.assertFalse(StudioBooking.objects.filter(pk=booking_id).exists())
                audit = AuditEntry.objects.get(object_type='studiobooking', object_id=booking_id,
                                               action='Удаление брони')
                self.assertEqual(audit.actor, self.owner)
                replacement = save_booking(self.owner, StudioBooking(
                    title='Освободившийся слот', studio=self.studio, responsible=self.owner,
                    starts_at=start, ends_at=end,
                ))
                self.assertNotEqual(replacement.pk, booking_id)
                self.assertEqual(replacement.status, status)
                StudioBooking.objects.filter(pk=replacement.pk).delete()

    def test_managers_can_delete_foreign_completed_and_legacy_cancelled_bookings(self):
        for role, status in [('director', 'completed'), ('radio_chief', 'cancelled'),
                             ('tv_chief', 'planned')]:
            with self.subTest(role=role, status=status):
                entry = self.booking(status=status)
                booking_id = entry.pk
                self.client.force_login(self.users[role])
                detail = self.client.get(entry.get_absolute_url())
                self.assertContains(detail, self.delete_url(entry))
                self.assertContains(detail, 'Удалить бронь')
                response = self.client.post(self.delete_url(entry))
                self.assertEqual(response.status_code, 302)
                self.assertFalse(StudioBooking.objects.filter(pk=booking_id).exists())
                self.assertTrue(AuditEntry.objects.filter(object_type='studiobooking', object_id=booking_id,
                                                         actor=self.users[role], action='Удаление брони').exists())

    def test_foreign_creator_assigned_engineer_and_owner_of_finished_booking_cannot_delete(self):
        for case, actor, kwargs in [
            ('foreign creator', self.users['tv_reporter'], {}),
            ('assigned engineer', self.users['engineer'], {'kind': 'video', 'engineer': self.users['engineer']}),
            ('finished own booking', self.owner, {'status': 'completed'}),
            ('expired own booking', self.owner, {'starts_at': self.now - timedelta(hours=2),
                                                'ends_at': self.now - timedelta(hours=1)}),
        ]:
            with self.subTest(case=case):
                entry = self.booking(**kwargs)
                with self.assertRaises(PermissionDenied):
                    delete_booking(actor, entry)
                self.client.force_login(actor)
                self.assertEqual(self.client.post(self.delete_url(entry)).status_code, 403)
                self.assertTrue(StudioBooking.objects.filter(pk=entry.pk).exists())
                detail = self.client.get(entry.get_absolute_url())
                self.assertNotContains(detail, self.delete_url(entry))
                self.assertFalse(AuditEntry.objects.filter(object_type='studiobooking', object_id=entry.pk,
                                                          action='Удаление брони').exists())

    def test_delete_is_post_only_csrf_protected_and_returns_to_local_calendar_day(self):
        # 06:30 locally is on the previous UTC date, so the return day must be local.
        start = timezone.localtime(self.now).replace(hour=6, minute=30) + timedelta(days=1)
        entry = self.booking(starts_at=start, ends_at=start + timedelta(hours=1))
        booking_id = entry.pk
        url = self.delete_url(entry)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.get(url).status_code, 405)
        detail = client.get(entry.get_absolute_url())
        self.assertContains(detail, 'data-confirm="Удалить бронь?"')
        self.assertContains(detail, 'class="button button-danger"')
        self.assertEqual(client.post(url).status_code, 403)
        self.assertTrue(StudioBooking.objects.filter(pk=booking_id).exists())
        response = client.post(url, {'csrfmiddlewaretoken': client.cookies['csrftoken'].value})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlsplit(response.url).path, reverse('newsroom:bookings'))
        self.assertEqual(parse_qs(urlsplit(response.url).query),
                         {'date': [timezone.localtime(start).date().isoformat()], 'view': ['week']})
        self.assertFalse(StudioBooking.objects.filter(pk=booking_id).exists())
        self.assertEqual(client.post(url, {'csrfmiddlewaretoken': client.cookies['csrftoken'].value}).status_code, 404)

    def test_delete_detaches_approved_proposal_without_erasing_review_or_history(self):
        proposal = save_proposal(self.users['producer'], Proposal(
            kind='booking', title='Предложенная запись', studio=self.studio,
            starts_at=self.now + timedelta(hours=1), ends_at=self.now + timedelta(hours=2),
        ))
        proposal = review_proposal(self.chief, proposal, True)
        booking_id = proposal.booking_id
        before = Proposal.objects.values().get(pk=proposal.pk)
        history = list(AuditEntry.objects.filter(object_type='proposal', object_id=proposal.pk)
                       .values_list('pk', 'actor_id', 'action'))
        delete_booking(self.chief, proposal.booking)
        proposal.refresh_from_db()
        self.assertIsNone(proposal.booking_id)
        self.assertFalse(StudioBooking.objects.filter(pk=booking_id).exists())
        after = Proposal.objects.values().get(pk=proposal.pk)
        self.assertEqual(after, {**before, 'booking_id': None})
        self.assertEqual(proposal.status, 'approved')
        self.assertEqual(list(AuditEntry.objects.filter(object_type='proposal', object_id=proposal.pk)
                              .values_list('pk', 'actor_id', 'action')), history)
        self.assertTrue(AuditEntry.objects.filter(object_type='studiobooking', object_id=booking_id,
                                                 action='Удаление брони').exists())
        self.assertTrue(Notification.objects.filter(url=proposal.get_absolute_url()).exists())

    def test_forged_cancel_is_rejected_without_changing_or_deleting_booking(self):
        entry = self.booking()
        for actor in [self.owner, self.chief]:
            with self.subTest(actor=actor.username):
                with self.assertRaises(ValidationError):
                    set_booking_status(actor, entry, 'cancelled')
                self.client.force_login(actor)
                for payload in [{'action': 'status', 'status': 'cancelled'}, {'action': 'cancel'}]:
                    response = self.client.post(reverse('newsroom:booking_action', args=[entry.pk]),
                                                payload, follow=True)
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, 'message-error')
                    self.assertRedirects(response, reverse('newsroom:bookings'))
                    detail = self.client.get(entry.get_absolute_url())
                    self.assertFalse(any(action.get('status') == 'cancelled' for action in detail.context['actions']))
                    self.assertNotContains(response, 'Отменить бронь')
                    self.assertNotContains(response, 'Отменено')
                entry.refresh_from_db()
                self.assertEqual(entry.status, 'planned')

    def test_legacy_cancelled_record_remains_in_db_but_is_hidden_from_schedules_and_status_api(self):
        legacy = self.booking(title='Старая отменённая запись', status='cancelled')
        visible = self.booking(title='Видимая запись', starts_at=self.now + timedelta(hours=3),
                               ends_at=self.now + timedelta(hours=4))
        day = timezone.localtime(legacy.starts_at).date().isoformat()
        for name, query in [
            ('bookings', {'date': day}),
            ('booking_create', {'date': day, 'studio': self.studio.pk}),
        ]:
            with self.subTest(page=name):
                response = self.client.get(reverse(f'newsroom:{name}'), query)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, visible.title)
                self.assertNotContains(response, legacy.title)
                self.assertNotContains(response, 'Отменено')
        day_response = self.client.get(reverse('newsroom:booking_day_schedule'), {'date': day, 'studio': self.studio.pk})
        self.assertEqual([row['id'] for row in day_response.json()['bookings']], [visible.pk])
        status_response = self.client.get(reverse('newsroom:booking_statuses'), {'ids': f'{legacy.pk},{visible.pk}'})
        self.assertEqual([row['id'] for row in status_response.json()['bookings']], [visible.pk])
        self.assertEqual(status_response['Cache-Control'], 'private, no-store')
        self.client.force_login(self.chief)
        detail = self.client.get(legacy.get_absolute_url())
        self.assertContains(detail, legacy.title)
        self.assertContains(detail, self.delete_url(legacy))
        self.assertNotContains(detail, 'Отменено')
        self.assertNotContains(detail, 'badge-cancelled')
        legacy.refresh_from_db()
        self.assertEqual(legacy.status, 'cancelled')

    def test_delete_cleans_only_exact_booking_notifications_and_later_ticks_never_recreate_it(self):
        entry = self.booking(starts_at=self.now - timedelta(minutes=30), ends_at=self.now + timedelta(minutes=30))
        booking_id, booking_url = entry.pk, entry.get_absolute_url()
        for recipient in [self.owner, self.users['engineer']]:
            Notification.objects.create(recipient=recipient, title='Ссылка на удалённую запись', url=booking_url)
        keep = Notification.objects.create(recipient=self.owner, title='Другая ссылка', url=f'{booking_url}edit/')
        delete_booking(self.owner, entry)
        self.assertFalse(Notification.objects.filter(url=booking_url).exists())
        self.assertTrue(Notification.objects.filter(pk=keep.pk).exists())
        self.mock_now.return_value = self.now + timedelta(hours=3)
        self.assertEqual(sync_booking_statuses(booking_ids=[booking_id]), 0)
        call_command('sync_booking_statuses', stdout=StringIO())
        response = self.client.get(reverse('newsroom:booking_statuses'), {'ids': str(booking_id)})
        self.assertEqual(response.json()['bookings'], [])
        self.assertFalse(StudioBooking.objects.filter(pk=booking_id).exists())
        self.assertFalse(Notification.objects.filter(url=booking_url).exists())
        self.assertEqual(AuditEntry.objects.filter(object_type='studiobooking', object_id=booking_id,
                                                  action='Удаление брони').count(), 1)
