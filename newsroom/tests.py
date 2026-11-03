from datetime import timedelta
from io import StringIO
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    BroadcastShift, Department, Event, EventAssignment, EventStatusHistory,
    Notification, Proposal, Role, Studio, StudioBooking, UploadedFile,
)
from .permissions import ROLE_DEPARTMENTS, assign_role, can, role_of, setup_roles
from .services import (
    claim_booking, complete_booking_montage, delete_upload, review_proposal,
    save_booking, save_employee, save_event, save_proposal, save_upload,
    set_assignment_status, set_booking_status, transition_event,
)

User = get_user_model()
TEST_PASSWORD = 'a-test-only-credential-943!'


class NewsroomTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        cls.users = {}
        for role in Role.values:
            user = User.objects.create_user(username=role, password=TEST_PASSWORD, first_name=f'Сотрудник {role}')
            assign_role(user, role, Department.objects.get(slug=ROLE_DEPARTMENTS[role]))
            cls.users[role] = user
        cls.studio = Studio.objects.create(name='Студия №1')
        cls.other_studio = Studio.objects.create(name='Студия №2')

    def setUp(self):
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.start = (timezone.localtime() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)

    def booking(self, **kwargs):
        values = dict(title='Запись интервью', studio=self.studio, starts_at=self.start,
                      ends_at=self.start + timedelta(hours=1), kind='audio',
                      responsible=self.users['radio_reporter'], created_by=self.users['radio_reporter'])
        values.update(kwargs)
        return StudioBooking(**values)

    def event(self, **kwargs):
        values = dict(title='Городская премьера', starts_at=self.start,
                      ends_at=self.start + timedelta(hours=2), created_by=self.users['tv_chief'],
                      reporter=self.users['tv_reporter'], camera=self.users['operator'],
                      photographer=self.users['photographer'], editor=self.users['editor'])
        values.update(kwargs)
        return Event.objects.create(**values)

    def upload(self, event=None, user=None, filename='material.txt'):
        upload = UploadedFile(event=event, uploaded_by=user or self.users['tv_reporter'],
                              file=SimpleUploadedFile(filename, b'newsroom material', content_type='text/plain'))
        return save_upload(user or self.users['tv_reporter'], upload)

    def login_as(self, role):
        self.client.force_login(self.users[role])


class AuthenticationTests(NewsroomTestCase):
    def test_correct_password_logs_in(self):
        self.assertTrue(self.client.login(username='radio_reporter', password=TEST_PASSWORD))
        self.assertEqual(self.client.get(reverse('newsroom:dashboard')).status_code, 200)

    def test_wrong_password_does_not_log_in(self):
        self.assertFalse(self.client.login(username='radio_reporter', password='incorrect'))
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_anonymous_cannot_open_internal_pages(self):
        for name in ['dashboard', 'shifts', 'bookings', 'events', 'files', 'employees', 'proposals', 'notifications', 'profile']:
            with self.subTest(page=name):
                response = self.client.get(reverse(f'newsroom:{name}'))
                self.assertEqual(response.status_code, 302)
                self.assertIn('next=', response.url)

    def test_inactive_user_cannot_log_in(self):
        user = self.users['radio_reporter']
        user.is_active = False
        user.save(update_fields=['is_active'])
        self.assertFalse(self.client.login(username=user.username, password=TEST_PASSWORD))

    def test_missing_or_mismatched_group_denies_permissions(self):
        user = self.users['radio_reporter']
        user.groups.clear()
        self.assertIsNone(role_of(user))
        self.assertFalse(can(user, 'bookings_create'))

    def test_csrf_required_for_mutations(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.users['radio_reporter'])
        response = client.post(reverse('newsroom:booking_create'), {})
        self.assertEqual(response.status_code, 403)


class BookingTests(NewsroomTestCase):
    def test_radio_reporter_cannot_edit_broadcast_schedule(self):
        self.login_as('radio_reporter')
        self.assertEqual(self.client.get(reverse('newsroom:shift_create')).status_code, 403)
        self.assertEqual(self.client.post(reverse('newsroom:shift_create'), {}).status_code, 403)
        self.assertEqual(BroadcastShift.objects.count(), 0)

    def test_radio_reporter_can_create_booking(self):
        booking = save_booking(self.users['radio_reporter'], self.booking())
        self.assertTrue(StudioBooking.objects.filter(pk=booking.pk).exists())
        self.assertEqual(booking.created_by, self.users['radio_reporter'])

    def test_radio_reporter_http_booking_persists(self):
        self.login_as('radio_reporter')
        response = self.client.post(reverse('newsroom:booking_create'), {
            'title': 'Прямой эфир', 'description': 'Интервью', 'studio': self.studio.pk,
            'date': timezone.localtime(self.start).date().isoformat(),
            'start_time': '10:00',
            'end_time': '11:00',
            'kind': 'audio', 'responsible': self.users['radio_reporter'].pk,
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(StudioBooking.objects.filter(title='Прямой эфир').exists())

    def test_overlapping_active_bookings_rejected(self):
        save_booking(self.users['radio_reporter'], self.booking())
        overlapping = self.booking(starts_at=self.start + timedelta(minutes=30))
        with self.assertRaises(ValidationError):
            save_booking(self.users['radio_reporter'], overlapping)
        self.assertEqual(StudioBooking.objects.count(), 1)

    def test_same_time_different_studios_allowed(self):
        save_booking(self.users['radio_reporter'], self.booking())
        save_booking(self.users['radio_reporter'], self.booking(studio=self.other_studio))
        self.assertEqual(StudioBooking.objects.count(), 2)

    def test_adjacent_bookings_allowed(self):
        save_booking(self.users['radio_reporter'], self.booking())
        save_booking(self.users['radio_reporter'], self.booking(starts_at=self.start + timedelta(hours=1), ends_at=self.start + timedelta(hours=2)))
        self.assertEqual(StudioBooking.objects.count(), 2)

    def test_cancelled_booking_does_not_block_slot(self):
        booking = save_booking(self.users['radio_reporter'], self.booking())
        set_booking_status(self.users['radio_reporter'], booking, 'cancelled')
        save_booking(self.users['radio_reporter'], self.booking())
        self.assertEqual(StudioBooking.objects.count(), 2)

    def test_invalid_time_range_rejected(self):
        with self.assertRaises(ValidationError):
            save_booking(self.users['radio_reporter'], self.booking(ends_at=self.start))

    def test_engineer_cannot_change_booking_fields(self):
        booking = save_booking(self.users['radio_reporter'], self.booking(kind='video'))
        booking.title = 'Несанкционированная правка'
        with self.assertRaises(PermissionDenied):
            save_booking(self.users['engineer'], booking)
        booking.refresh_from_db()
        self.assertEqual(booking.title, 'Запись интервью')
        self.login_as('engineer')
        response = self.client.post(reverse('newsroom:booking_edit', args=[booking.pk]), {'title': 'Подмена'})
        self.assertEqual(response.status_code, 403)

    def test_video_booking_notifies_engineer(self):
        booking = save_booking(self.users['radio_reporter'], self.booking(kind='video'))
        self.assertTrue(Notification.objects.filter(recipient=self.users['engineer'], url=booking.get_absolute_url()).exists())

    def test_only_claimed_engineer_can_mark_montage(self):
        booking = save_booking(self.users['radio_reporter'], self.booking(kind='video'))
        with self.assertRaises(PermissionDenied):
            complete_booking_montage(self.users['engineer'], booking)
        claim_booking(self.users['engineer'], booking)
        booking.refresh_from_db()
        self.assertEqual(booking.engineer, self.users['engineer'])
        set_booking_status(self.users['radio_reporter'], booking, 'completed')
        complete_booking_montage(self.users['engineer'], booking)
        booking.refresh_from_db()
        self.assertTrue(booking.montage_completed)
        self.assertEqual(booking.status, 'completed')

    def test_completed_booking_sends_studio_available_notification(self):
        booking = save_booking(self.users['radio_reporter'], self.booking())
        set_booking_status(self.users['radio_reporter'], booking, 'recording')
        set_booking_status(self.users['radio_reporter'], booking, 'completed')
        self.assertTrue(Notification.objects.filter(url=booking.get_absolute_url()).exists())

    def test_site_editor_cannot_create_or_edit_booking(self):
        self.login_as('site_editor')
        self.assertEqual(self.client.post(reverse('newsroom:booking_create'), {}).status_code, 403)


class WorkflowTests(NewsroomTestCase):
    def test_assigning_event_sends_notifications(self):
        event = Event(title='Новый инфоповод', starts_at=self.start, ends_at=self.start + timedelta(hours=1),
                      reporter=self.users['tv_reporter'], editor=self.users['editor'])
        event = save_event(self.users['tv_chief'], event)
        self.assertTrue(Notification.objects.filter(recipient=self.users['tv_reporter'], url=event.get_absolute_url()).exists())

    def test_only_assigned_reporter_can_complete_shooting(self):
        event = self.event(reporter=None)
        with self.assertRaises(PermissionDenied):
            transition_event(self.users['tv_reporter'], event, 'shot')
        event.reporter = self.users['tv_reporter']
        event.save(update_fields=['reporter'])
        transition_event(self.users['tv_reporter'], event, 'shot')
        event.refresh_from_db()
        self.assertEqual(event.status, 'shot')
        self.assertIsNotNone(event.shooting_completed_at)
        self.assertTrue(EventStatusHistory.objects.filter(event=event, new_status='shot', actor=self.users['tv_reporter']).exists())

    def test_materials_cannot_transfer_without_uploaded_files(self):
        event = self.event(status='shot', shooting_completed_at=timezone.now())
        with self.assertRaises(ValidationError):
            transition_event(self.users['tv_reporter'], event, 'transferred')
        event.refresh_from_db()
        self.assertEqual(event.status, 'shot')

    def test_editor_notification_delayed_until_shooting_plus_thirty_minutes(self):
        shot_at = timezone.now()
        event = self.event(status='shot', shooting_completed_at=shot_at)
        self.upload(event)
        transition_event(self.users['tv_reporter'], event, 'transferred')
        notification = Notification.objects.get(recipient=self.users['editor'], url=event.get_absolute_url())
        self.assertGreaterEqual(notification.available_at, shot_at + timedelta(minutes=30))
        self.assertGreater(notification.available_at, timezone.now())

    def test_editor_notification_never_precedes_material_transfer(self):
        event = self.event(status='shot', shooting_completed_at=timezone.now() - timedelta(hours=2))
        self.upload(event)
        before_transfer = timezone.now()
        transition_event(self.users['tv_reporter'], event, 'transferred')
        notification = Notification.objects.get(recipient=self.users['editor'], url=event.get_absolute_url())
        self.assertGreaterEqual(notification.available_at, before_transfer)

    def test_editor_can_only_complete_assigned_video(self):
        event = self.event(status='transferred', editor=None)
        with self.assertRaises(PermissionDenied):
            transition_event(self.users['editor'], event, 'edited')
        event.editor = self.users['editor']
        event.save(update_fields=['editor'])
        transition_event(self.users['editor'], event, 'edited')
        event.refresh_from_db()
        self.assertEqual(event.status, 'edited')
        self.assertTrue(Notification.objects.filter(recipient=self.users['tv_chief'], url=event.get_absolute_url()).exists())

    def test_only_tv_chief_or_director_can_approve_video(self):
        for forbidden_role in ['editor', 'tv_reporter', 'radio_chief', 'site_chief', 'site_editor', 'producer']:
            with self.subTest(role=forbidden_role):
                event = self.event(status='edited')
                with self.assertRaises(PermissionDenied):
                    transition_event(self.users[forbidden_role], event, 'approved')
        for permitted_role in ['tv_chief', 'director']:
            with self.subTest(role=permitted_role):
                event = self.event(status='edited')
                transition_event(self.users[permitted_role], event, 'approved')
                event.refresh_from_db()
                self.assertEqual(event.status, 'approved')

    def test_approval_notifies_site_and_publication_requires_approval(self):
        event = self.event(status='edited')
        with self.assertRaises(ValidationError):
            transition_event(self.users['site_editor'], event, 'published')
        transition_event(self.users['tv_chief'], event, 'approved')
        self.assertTrue(Notification.objects.filter(recipient=self.users['site_editor'], url=event.get_absolute_url()).exists())
        transition_event(self.users['site_editor'], event, 'published')
        event.refresh_from_db()
        self.assertEqual(event.status, 'published')

    def test_photographer_changes_only_own_assignment(self):
        event = self.event()
        assignment = EventAssignment.objects.create(event=event, employee=self.users['photographer'], job='photographer')
        with self.assertRaises(PermissionDenied):
            set_assignment_status(self.users['operator'], event, 'completed')
        set_assignment_status(self.users['photographer'], event, 'completed')
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, 'completed')
        event.refresh_from_db()
        self.assertEqual(event.status, 'planned')

    def test_radio_reporter_cannot_open_tv_calendar(self):
        self.login_as('radio_reporter')
        self.assertEqual(self.client.get(reverse('newsroom:events')).status_code, 403)

    def test_calendar_time_does_not_advance_workflow(self):
        event = self.event(starts_at=timezone.now() - timedelta(hours=2), ends_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(event.temporal_status, 'Прошедшее мероприятие')
        event.refresh_from_db()
        self.assertEqual(event.status, 'planned')


class FileTests(NewsroomTestCase):
    def test_unauthorized_user_cannot_download_closed_file(self):
        event = self.event()
        upload = self.upload(event)
        for role in ['radio_reporter', 'producer', 'site_editor']:
            with self.subTest(role=role):
                self.login_as(role)
                response = self.client.get(reverse('newsroom:file_download', args=[upload.pk]))
                self.assertEqual(response.status_code, 403)
        self.login_as('editor')
        response = self.client.get(reverse('newsroom:file_download', args=[upload.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response['Content-Disposition'])
        response.close()

    def test_site_can_download_after_approval(self):
        event = self.event()
        upload = self.upload(event)
        event.status = 'approved'
        event.save(update_fields=['status'])
        self.login_as('site_editor')
        response = self.client.get(reverse('newsroom:file_download', args=[upload.pk]))
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_file_size_and_extension_validated(self):
        with override_settings(MAX_UPLOAD_SIZE=3):
            with self.assertRaises(ValidationError):
                self.upload()
        with self.assertRaises(ValidationError):
            self.upload(filename='payload.exe')
        self.assertEqual(UploadedFile.objects.count(), 0)

    def test_user_cannot_delete_another_users_file(self):
        upload = self.upload()
        with self.assertRaises(PermissionDenied):
            delete_upload(self.users['radio_reporter'], upload)
        self.assertTrue(UploadedFile.objects.filter(pk=upload.pk).exists())
        delete_upload(self.users['tv_reporter'], upload)
        self.assertFalse(UploadedFile.objects.filter(pk=upload.pk).exists())

    def test_public_media_url_is_not_served(self):
        upload = self.upload()
        self.assertEqual(self.client.get('/private-files-not-served/' + upload.file.name).status_code, 404)

    def test_transferred_attachment_requires_director_for_deletion(self):
        event = self.event()
        upload = self.upload(event)
        event.status = 'transferred'
        event.save(update_fields=['status'])
        with self.assertRaises(ValidationError):
            delete_upload(self.users['tv_reporter'], upload)
        self.assertTrue(UploadedFile.objects.filter(pk=upload.pk).exists())
        with self.captureOnCommitCallbacks(execute=True):
            delete_upload(self.users['director'], upload)
        self.assertFalse(UploadedFile.objects.filter(pk=upload.pk).exists())


class ProposalAndEmployeeTests(NewsroomTestCase):
    def proposal(self, **kwargs):
        values = dict(kind='event', title='Новая тема', starts_at=self.start, ends_at=self.start + timedelta(hours=1))
        values.update(kwargs)
        return save_proposal(self.users['producer'], Proposal(**values))

    def test_producer_cannot_approve_own_proposal(self):
        proposal = self.proposal()
        with self.assertRaises(PermissionDenied):
            review_proposal(self.users['producer'], proposal, True)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'pending')
        self.assertIsNone(proposal.event_id)
        self.login_as('producer')
        response = self.client.post(reverse('newsroom:proposal_review', args=[proposal.pk]), {'decision': 'approve'})
        self.assertEqual(response.status_code, 403)

    def test_approval_creates_event_once_and_notifies_producer(self):
        proposal = self.proposal()
        review_proposal(self.users['tv_chief'], proposal, True)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'approved')
        self.assertIsNotNone(proposal.event_id)
        self.assertTrue(Notification.objects.filter(recipient=self.users['producer'], url=proposal.get_absolute_url()).exists())
        with self.assertRaises(ValidationError):
            review_proposal(self.users['tv_chief'], proposal, True)
        self.assertEqual(Event.objects.count(), 1)

    def test_booking_proposal_conflict_keeps_pending(self):
        save_booking(self.users['radio_reporter'], self.booking())
        proposal = self.proposal(kind='booking', studio=self.studio)
        with self.assertRaises(ValidationError):
            review_proposal(self.users['radio_chief'], proposal, True)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'pending')
        self.assertIsNone(proposal.booking_id)

    def test_radio_chief_cannot_create_director(self):
        target = User(username='privilege_escalation', first_name='Новый директор')
        with self.assertRaises(PermissionDenied):
            save_employee(self.users['radio_chief'], target, 'director', Department.objects.get(slug='management'), TEST_PASSWORD)
        self.assertFalse(User.objects.filter(username='privilege_escalation').exists())
        self.login_as('radio_chief')
        response = self.client.post(reverse('newsroom:employee_create'), {
            'full_name': 'Новый директор', 'username': 'http_escalation', 'password': TEST_PASSWORD,
            'department': Department.objects.get(slug='management').pk, 'role': 'director', 'is_active': 'on',
        })
        self.assertIn(response.status_code, [200, 403])
        self.assertFalse(User.objects.filter(username='http_escalation').exists())

    def test_director_can_create_employee_with_hashed_password(self):
        target = User(username='new_editor', first_name='Новый редактор')
        saved = save_employee(self.users['director'], target, 'site_editor', Department.objects.get(slug='site'), TEST_PASSWORD)
        saved.refresh_from_db()
        self.assertTrue(saved.check_password(TEST_PASSWORD))
        self.assertNotEqual(saved.password, TEST_PASSWORD)
        self.assertEqual(role_of(saved), 'site_editor')

    def test_ordinary_employee_cannot_manage_accounts(self):
        self.login_as('tv_reporter')
        self.assertEqual(self.client.get(reverse('newsroom:employees')).status_code, 403)
        self.assertEqual(self.client.post(reverse('newsroom:employee_create'), {}).status_code, 403)


class NotificationAndSmokeTests(NewsroomTestCase):
    def test_delayed_notification_is_hidden_from_poll_and_read(self):
        hidden = Notification.objects.create(recipient=self.users['editor'], title='Ещё рано', available_at=timezone.now() + timedelta(minutes=30))
        self.login_as('editor')
        response = self.client.get(reverse('newsroom:notification_poll'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Ещё рано')
        response = self.client.post(reverse('newsroom:notification_read', args=[hidden.pk]))
        self.assertIn(response.status_code, [403, 404])
        hidden.refresh_from_db()
        self.assertFalse(hidden.is_read)

    def test_notification_cannot_be_read_by_another_user(self):
        notification = Notification.objects.create(recipient=self.users['tv_reporter'], title='Личное уведомление')
        self.login_as('producer')
        response = self.client.post(reverse('newsroom:notification_read', args=[notification.pk]))
        self.assertIn(response.status_code, [403, 404])
        notification.refresh_from_db()
        self.assertFalse(notification.is_read)

    def test_all_role_dashboards_and_allowed_pages_render(self):
        capabilities = [('shifts_view', 'shifts'), ('bookings_view', 'bookings'), ('events_view', 'events'),
                        ('files_view', 'files'), ('employees_manage', 'employees'), ('proposals_view', 'proposals'), ('news_view', 'news')]
        for role, user in self.users.items():
            self.client.force_login(user)
            for page in ['dashboard', 'profile', 'notifications'] + [page for cap, page in capabilities if can(user, cap)]:
                with self.subTest(role=role, page=page):
                    self.assertEqual(self.client.get(reverse(f'newsroom:{page}')).status_code, 200)


class ManagementCommandTests(NewsroomTestCase):
    def test_seed_demo_is_idempotent_and_preserves_passwords(self):
        output = StringIO()
        call_command('seed_demo', stdout=output)
        counts = (User.objects.count(), BroadcastShift.objects.count(), StudioBooking.objects.count(), Event.objects.count(), Proposal.objects.count(), UploadedFile.objects.count(), Notification.objects.count())
        self.assertEqual(User.objects.filter(username__startswith='demo_').count(), len(Role.values))
        user = User.objects.get(username='demo_director')
        password_hash = user.password
        self.assertTrue(user.has_usable_password())
        self.assertEqual(role_of(user), 'director')
        second_output = StringIO()
        call_command('seed_demo', stdout=second_output)
        self.assertEqual(counts, (User.objects.count(), BroadcastShift.objects.count(), StudioBooking.objects.count(), Event.objects.count(), Proposal.objects.count(), UploadedFile.objects.count(), Notification.objects.count()))
        user.refresh_from_db()
        self.assertEqual(user.password, password_hash)
        self.assertNotIn(' / ', second_output.getvalue())
        self.assertEqual(Studio.objects.count(), 2)

    def test_create_director_generates_hashed_credentials_and_refuses_existing_user(self):
        from django.core.management.base import CommandError
        output = StringIO()
        call_command('create_director', username='first_director', name='Руководитель редакции', generate_password=True, stdout=output)
        user = User.objects.get(username='first_director')
        self.assertEqual(role_of(user), 'director')
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)
        generated = output.getvalue().split('Пароль (выводится один раз): ')[1].strip()
        self.assertTrue(user.check_password(generated))
        original_hash = user.password
        with self.assertRaises(CommandError):
            call_command('create_director', username='first_director', generate_password=True, stdout=StringIO())
        user.refresh_from_db()
        self.assertEqual(user.password, original_hash)


    def test_seed_refuses_conflicting_unmarked_account(self):
        from django.core.management.base import CommandError
        user = User.objects.create_user(username='demo_director', password=TEST_PASSWORD)
        before_count = User.objects.count()
        with self.assertRaises(CommandError):
            call_command('seed_demo', stdout=StringIO())
        self.assertEqual(User.objects.count(), before_count)
        user.refresh_from_db()
        self.assertTrue(user.check_password(TEST_PASSWORD))
        self.assertFalse(Event.objects.exists())


class HttpWorkflowTests(NewsroomTestCase):
    def test_full_material_workflow_through_http_forms(self):
        self.login_as('tv_chief')
        response = self.client.post(reverse('newsroom:event_create'), {
            'title': 'Репортаж через формы', 'description': 'Полный производственный цикл',
            'location': 'Городской пресс-центр',
            'starts_at': timezone.localtime(self.start).strftime('%Y-%m-%dT%H:%M'),
            'ends_at': timezone.localtime(self.start + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M'),
            'reporter': self.users['tv_reporter'].pk, 'camera': self.users['operator'].pk,
            'photographer': self.users['photographer'].pk, 'editor': self.users['editor'].pk,
            'status': 'approved',
        })
        self.assertEqual(response.status_code, 302)
        event = Event.objects.get(title='Репортаж через формы')
        self.assertEqual(event.status, 'planned')
        self.assertEqual(event.assignments.count(), 2)
        self.login_as('tv_reporter')
        response = self.client.post(reverse('newsroom:event_action', args=[event.pk]), {'action': 'shot'})
        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.status, 'shot')
        response = self.client.post(reverse('newsroom:file_upload'), {
            'title': 'Исходные материалы', 'event': event.pk,
            'file': SimpleUploadedFile('reportage.txt', b'real file attached to event'),
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(event.files.count(), 1)
        for role, action in [('tv_reporter', 'transferred'), ('editor', 'edited'), ('tv_chief', 'approved'), ('site_editor', 'published')]:
            with self.subTest(stage=action):
                self.login_as(role)
                response = self.client.post(reverse('newsroom:event_action', args=[event.pk]), {'action': action})
                self.assertEqual(response.status_code, 302)
                event.refresh_from_db()
                self.assertEqual(event.status, action)
                self.assertEqual(self.client.get(event.get_absolute_url()).status_code, 200)
        self.assertEqual(list(event.history.values_list('new_status', flat=True)), list(Event.Status.values))
        self.assertTrue(Notification.objects.filter(recipient=self.users['site_editor'], url=event.get_absolute_url()).exists())

    def test_director_creates_employee_through_http_form(self):
        self.login_as('director')
        response = self.client.post(reverse('newsroom:employee_create'), {
            'full_name': 'Новый сотрудник сайта', 'username': 'http_new_employee',
            'password': TEST_PASSWORD, 'department': Department.objects.get(slug='site').pk,
            'role': 'site_editor', 'is_active': 'on', 'is_superuser': 'on',
        })
        self.assertEqual(response.status_code, 302)
        employee = User.objects.get(username='http_new_employee')
        self.assertTrue(employee.check_password(TEST_PASSWORD))
        self.assertEqual(role_of(employee), 'site_editor')
        self.assertFalse(employee.is_superuser)
        self.assertEqual(employee.get_full_name(), 'Новый сотрудник сайта')

    def test_login_and_logout_forms(self):
        response = self.client.post(reverse('newsroom:login'), {'username': 'radio_reporter', 'password': 'bad-password'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)
        response = self.client.post(reverse('newsroom:login'), {'username': 'radio_reporter', 'password': TEST_PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)
        response = self.client.post(reverse('newsroom:logout'))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('_auth_user_id', self.client.session)
