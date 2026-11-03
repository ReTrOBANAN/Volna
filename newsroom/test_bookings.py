"""Studio date/range forms and their scoped daily schedule keep server guarantees."""
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Department, Proposal, Studio, StudioBooking
from .permissions import assign_role, setup_roles
from .services import save_booking, save_proposal, set_booking_status

User = get_user_model()
LOCAL_ZONE = ZoneInfo('Asia/Novosibirsk')


class BookingLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))


@override_settings(TIME_ZONE='Asia/Novosibirsk')
class BookingCalendarFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        cls.users = {}
        for role, slug in [('radio_chief', 'radio'), ('radio_reporter', 'radio'),
                           ('engineer', 'tv'), ('operator', 'tv'), ('site_editor', 'site')]:
            user = User.objects.create_user(username=f'booking_{role}', first_name=f'Сотрудник {role}')
            assign_role(user, role, Department.objects.get(slug=slug))
            cls.users[role] = user
        cls.studio = Studio.objects.create(name='Студия формы №1')
        cls.other_studio = Studio.objects.create(name='Студия формы №2')

    def setUp(self):
        frozen = patch('django.utils.timezone.now', return_value=datetime(2027, 3, 1, 3, tzinfo=datetime_timezone.utc))
        frozen.start()
        self.addCleanup(frozen.stop)
        self.reporter = self.users['radio_reporter']
        self.chief = self.users['radio_chief']
        self.client.force_login(self.reporter)
        self.day = date(2027, 3, 12)
        self.create_url = reverse('newsroom:booking_create')
        self.calendar_url = reverse('newsroom:bookings')
        self.api_url = reverse('newsroom:booking_day_schedule')

    def moment(self, day, hour, minute=0):
        return datetime.combine(day, time(hour, minute), tzinfo=LOCAL_ZONE)

    def booking(self, **kwargs):
        values = {
            'title': 'Сохранённая запись', 'description': '', 'studio': self.studio,
            'starts_at': self.moment(self.day, 10), 'ends_at': self.moment(self.day, 11),
            'kind': 'audio', 'responsible': self.reporter, 'created_by': self.reporter,
        }
        values.update(kwargs)
        return StudioBooking.objects.create(**values)

    def data(self, **kwargs):
        values = {
            'title': 'Новая запись', 'description': 'Интервью', 'studio': self.studio.pk,
            'date': self.day.isoformat(), 'start_time': '10:00', 'end_time': '11:00',
            'kind': 'audio', 'responsible': self.reporter.pk,
        }
        values.update(kwargs)
        return values

    def test_calendar_day_links_seed_date_studio_and_native_range_inputs(self):
        response = self.client.get(self.calendar_url, {'date': self.day.isoformat(), 'view': 'week'})
        self.assertEqual(response.status_code, 200)
        parsed = BookingLinks()
        parsed.feed(response.content.decode())
        adds = [link['href'] for link in parsed.links if urlsplit(link.get('href', '')).path == self.create_url]
        self.assertEqual(len(adds), 14, 'each day of each studio has its own add action')
        self.assertTrue(all(parse_qs(urlsplit(link).query).get('date') and parse_qs(urlsplit(link).query).get('studio') for link in adds))
        response = self.client.get(self.create_url, {'date': self.day.isoformat(), 'studio': self.other_studio.pk})
        self.assertEqual(response.status_code, 200)
        form = response.context['form']
        self.assertEqual(str(form['date'].value()), self.day.isoformat())
        self.assertEqual(str(form['studio'].value()), str(self.other_studio.pk))
        self.assertContains(response, 'name="start_time"')
        self.assertContains(response, 'name="end_time"')
        self.assertNotContains(response, 'name="end_date"')
        self.assertNotIn('end_date', form.fields)
        self.assertNotContains(response, 'name="starts_at"')
        self.assertNotContains(response, 'type="datetime-local"')

    def test_create_stores_local_times_and_ignores_forged_old_and_workflow_fields(self):
        response = self.client.post(self.create_url, self.data(
            start_time='00:30', end_time='02:00', starts_at='2030-01-01T18:00',
            ends_at='2030-01-01T19:00', status='completed', engineer=self.users['engineer'].pk,
            montage_completed='on',
        ))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlsplit(response.url).path, self.calendar_url)
        entry = StudioBooking.objects.get()
        self.assertEqual(entry.starts_at, self.moment(self.day, 0, 30))
        self.assertEqual(entry.ends_at, self.moment(self.day, 2))
        self.assertEqual(entry.status, 'planned')
        self.assertIsNone(entry.engineer_id)
        self.assertFalse(entry.montage_completed)
        self.assertEqual(entry.created_by, self.reporter)

    def test_legacy_multiday_record_changes_only_after_a_valid_same_day_edit(self):
        last_day = self.day + timedelta(days=2)
        entry = self.booking(studio=self.other_studio, starts_at=self.moment(self.day, 23), ends_at=self.moment(last_day, 6, 30))
        url = reverse('newsroom:booking_edit', args=[entry.pk])
        response = self.client.get(url, {'date': '2030-01-01', 'studio': self.studio.pk})
        form = response.context['form']
        self.assertEqual(str(form['date'].value()), self.day.isoformat())
        self.assertEqual(str(form['studio'].value()), str(self.other_studio.pk))
        self.assertNotIn('end_date', form.fields)
        self.assertIn('23:00', form['start_time'].as_widget())
        self.assertIn('06:30', form['end_time'].as_widget())
        entry.refresh_from_db()
        self.assertEqual(entry.ends_at, self.moment(last_day, 6, 30))
        response = self.client.post(url, self.data(studio=self.other_studio.pk, start_time='23:00', end_time='06:30'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('end_time', response.context['form'].errors)
        entry.refresh_from_db()
        self.assertEqual(entry.ends_at, self.moment(last_day, 6, 30))
        response = self.client.post(url, self.data(title='Изменённая запись', studio=self.other_studio.pk,
                                                   start_time='22:00', end_time='23:00'))
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.title, 'Изменённая запись')
        self.assertEqual(entry.starts_at, self.moment(self.day, 22))
        self.assertEqual(entry.ends_at, self.moment(self.day, 23))
        self.assertEqual(entry.created_by, self.reporter)

    def test_invalid_bound_edits_retain_input_without_overwriting_saved_booking(self):
        entry = self.booking()
        original = (entry.title, entry.starts_at, entry.ends_at, entry.studio_id)
        url = reverse('newsroom:booking_edit', args=[entry.pk])
        for data in [{}, self.data(title='Не потерять', start_time='13:00', end_time='12:00'),
                     self.data(date='not-a-date'), self.data(end_date=(self.day - timedelta(days=1)).isoformat()),
                     self.data(start_time='10:00', end_time='10:00')]:
            with self.subTest(data=data):
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 200)
                form = response.context['form']
                self.assertTrue(form.is_bound)
                self.assertTrue(form.errors)
                if 'title' in data:
                    self.assertEqual(form['title'].value(), data['title'])
                entry.refresh_from_db()
                self.assertEqual((entry.title, entry.starts_at, entry.ends_at, entry.studio_id), original)

    def test_http_conflict_is_rejected_and_saved_slot_stays_unchanged(self):
        existing = self.booking()
        response = self.client.post(self.create_url, self.data(start_time='10:30', end_time='11:30'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        self.assertContains(response, 'Студия уже занята')
        self.assertEqual(StudioBooking.objects.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.starts_at, self.moment(self.day, 10))

    def test_adjacent_nonblocking_and_other_studio_intervals_can_be_saved(self):
        self.booking(starts_at=self.moment(self.day, 8), ends_at=self.moment(self.day, 9))
        self.booking(status='cancelled', starts_at=self.moment(self.day, 10), ends_at=self.moment(self.day, 11))
        self.booking(status='completed', starts_at=self.moment(self.day, 12), ends_at=self.moment(self.day, 13))
        self.booking(starts_at=self.moment(self.day, 14), ends_at=self.moment(self.day, 15))
        cases = [('Соседний слот', '09:00', '10:00', self.studio.pk),
                 ('После отмены', '10:00', '11:00', self.studio.pk),
                 ('После завершения', '12:00', '13:00', self.studio.pk),
                 ('Другая студия', '14:00', '15:00', self.other_studio.pk)]
        for title, start, end, studio in cases:
            with self.subTest(title=title):
                response = self.client.post(self.create_url, self.data(title=title, start_time=start, end_time=end, studio=studio))
                self.assertEqual(response.status_code, 302)
                self.assertTrue(StudioBooking.objects.filter(title=title).exists())

    def test_responsible_choices_enforce_role_scope_and_allow_chief_assignment(self):
        response = self.client.post(self.create_url, self.data(responsible=self.chief.pk))
        self.assertEqual(response.status_code, 200)
        self.assertIn('responsible', response.context['form'].errors)
        self.assertFalse(StudioBooking.objects.exists())
        self.client.force_login(self.chief)
        response = self.client.post(self.create_url, self.data(responsible=self.reporter.pk))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StudioBooking.objects.get().responsible, self.reporter)

    def test_create_and_foreign_edit_keep_existing_role_permissions(self):
        other = self.booking(created_by=self.chief)
        response = self.client.post(reverse('newsroom:booking_edit', args=[other.pk]), self.data(title='Подмена'))
        self.assertEqual(response.status_code, 403)
        for role in ['engineer', 'site_editor']:
            self.client.force_login(self.users[role])
            self.assertEqual(self.client.get(self.create_url).status_code, 403)
            self.assertEqual(self.client.post(self.create_url, self.data()).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.create_url).status_code, 302)
        other.refresh_from_db()
        self.assertEqual(other.title, 'Сохранённая запись')

    def test_csrf_protected_form_accepts_real_token_and_rejects_missing_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.reporter)
        response = client.get(self.create_url, {'date': self.day.isoformat(), 'studio': self.studio.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.post(self.create_url, self.data()).status_code, 403)
        data = self.data(csrfmiddlewaretoken=client.cookies['csrftoken'].value)
        self.assertEqual(client.post(self.create_url, data).status_code, 302)
        self.assertEqual(StudioBooking.objects.count(), 1)

    def test_nonexistent_or_ambiguous_local_time_is_rejected(self):
        with timezone.override(ZoneInfo('Europe/Berlin')):
            for day in ['2027-03-28', '2027-10-31']:
                with self.subTest(day=day):
                    response = self.client.post(self.create_url, self.data(date=day, start_time='02:30', end_time='04:00'))
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('start_time', response.context['form'].errors)
        self.assertFalse(StudioBooking.objects.exists())

    def test_daily_api_scopes_studio_and_day_clips_multiday_and_excludes_edited_record(self):
        overnight = self.booking(starts_at=self.moment(self.day - timedelta(days=1), 23), ends_at=self.moment(self.day, 1))
        cancelled = self.booking(status='cancelled')
        completed = self.booking(status='completed', starts_at=self.moment(self.day, 12), ends_at=self.moment(self.day, 13))
        recording = self.booking(status='recording', starts_at=self.moment(self.day, 16), ends_at=self.moment(self.day, 17))
        later = self.booking(starts_at=self.moment(self.day, 23), ends_at=self.moment(self.day + timedelta(days=1), 1))
        self.booking(studio=self.other_studio)
        self.booking(starts_at=self.moment(self.day + timedelta(days=1), 8), ends_at=self.moment(self.day + timedelta(days=1), 9))
        response = self.client.get(self.api_url, {'date': self.day.isoformat(), 'studio': self.studio.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        data = response.json()
        self.assertEqual((data['date'], data['studio']), (self.day.isoformat(), self.studio.pk))
        rows = {row['id']: row for row in data['bookings']}
        self.assertEqual(set(rows), {overnight.pk, cancelled.pk, completed.pk, recording.pk, later.pk})
        self.assertEqual((rows[overnight.pk]['start_minute'], rows[overnight.pk]['end_minute']), (0, 60))
        self.assertEqual((rows[later.pk]['start_minute'], rows[later.pk]['end_minute']), (1380, 1440))
        self.assertIn('11.03', rows[overnight.pk]['start_label'])
        self.assertIn('13.03', rows[later.pk]['end_label'])
        self.assertTrue(rows[overnight.pk]['is_blocking'])
        self.assertTrue(rows[recording.pk]['is_blocking'])
        self.assertFalse(rows[cancelled.pk]['is_blocking'])
        self.assertFalse(rows[completed.pk]['is_blocking'])
        response = self.client.get(self.api_url, {'date': self.day.isoformat(), 'studio': self.studio.pk, 'exclude': overnight.pk})
        self.assertNotIn(overnight.pk, [row['id'] for row in response.json()['bookings']])

    def test_daily_api_validates_inputs_and_authorization(self):
        valid = {'date': self.day.isoformat(), 'studio': self.studio.pk}
        for query in [{}, {'date': '2027-02-30', 'studio': self.studio.pk},
                      {'date': self.day.isoformat(), 'studio': 'bad-id'},
                      {'date': self.day.isoformat(), 'studio': 99999999},
                      {**valid, 'exclude': 'bad-id'}]:
            with self.subTest(query=query):
                self.assertEqual(self.client.get(self.api_url, query).status_code, 400)
        self.assertEqual(self.client.post(self.api_url, valid).status_code, 405)
        self.client.force_login(self.users['engineer'])
        self.assertEqual(self.client.get(self.api_url, valid).status_code, 200)
        self.client.force_login(self.users['operator'])
        self.assertEqual(self.client.get(self.api_url, valid).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.api_url, valid).status_code, 302)

    def test_invalid_post_preview_tracks_posted_date_and_studio_and_excludes_original(self):
        entry = self.booking()
        changed_day = self.day + timedelta(days=1)
        unsafe_title = '</script><script>alert("booking-preview")</script>'
        other = self.booking(title=unsafe_title, studio=self.other_studio, starts_at=self.moment(changed_day, 8), ends_at=self.moment(changed_day, 9))
        url = reverse('newsroom:booking_edit', args=[entry.pk])
        response = self.client.post(url, self.data(date=changed_day.isoformat(), studio=self.other_studio.pk, start_time='16:00', end_time='13:00'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['schedule_date'], changed_day)
        self.assertEqual(response.context['schedule_studio'].pk, self.other_studio.pk)
        self.assertEqual([row['id'] for row in response.context['day_schedule']], [other.pk])
        self.assertEqual(response.context['day_schedule'][0]['title'], unsafe_title)
        self.assertNotContains(response, '<script>alert("booking-preview")</script>')
        self.assertEqual(response.context['form']['start_time'].value(), '16:00')
        entry.refresh_from_db()
        self.assertEqual(entry.studio, self.studio)
        self.assertEqual(entry.starts_at, self.moment(self.day, 10))
        response = self.client.post(url, self.data(studio='', date=changed_day.isoformat()))
        self.assertEqual(response.status_code, 200)
        self.assertIn('studio', response.context['form'].errors)
        self.assertIsNone(response.context['schedule_studio'])
        self.assertEqual(response.context['day_schedule'], [])

    def test_calendar_redirect_retains_selected_period_and_view_or_uses_saved_day(self):
        cases = [({'period': '2027-03-08', 'view': 'week'}, '2027-03-12', '2027-03-08', 'week'),
                 ({'period': '2027-03-12', 'view': 'day'}, '2027-03-13', '2027-03-13', 'day'),
                 ({'period': 'https://example.org/', 'view': 'unknown'}, '2027-03-14', '2027-03-14', 'week')]
        for index, (query, day, expected, view) in enumerate(cases):
            with self.subTest(query=query):
                response = self.client.post(f'{self.create_url}?{urlencode(query)}', self.data(title=f'Период {index}', date=day))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(urlsplit(response.url).path, self.calendar_url)
                self.assertEqual(parse_qs(urlsplit(response.url).query)['date'], [expected])
                self.assertEqual(parse_qs(urlsplit(response.url).query)['view'], [view])


    def test_cross_day_booking_is_rejected_by_form_and_service_using_local_calendar_days(self):
        next_day = self.day + timedelta(days=1)
        for start, end in [('23:00', '01:00'), ('10:00', '11:00')]:
            with self.subTest(start=start, end=end):
                response = self.client.post(self.create_url, self.data(start_time=start, end_time=end, end_date=next_day.isoformat()))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['form'].non_field_errors())
                self.assertFalse(StudioBooking.objects.exists())
        night = StudioBooking(title='Через полночь', studio=self.studio, responsible=self.reporter,
                              starts_at=self.moment(self.day, 23), ends_at=self.moment(next_day, 1))
        with self.assertRaises(ValidationError):
            save_booking(self.reporter, night)
        self.assertFalse(StudioBooking.objects.exists())
        same_day = StudioBooking(title='Один местный день', studio=self.studio, responsible=self.reporter,
                                 starts_at=self.moment(self.day, 6, 30), ends_at=self.moment(self.day, 8))
        saved = save_booking(self.reporter, same_day)
        saved.refresh_from_db()
        self.assertNotEqual(saved.starts_at.date(), saved.ends_at.date(), 'UTC dates may differ within one local day')
        self.assertEqual(timezone.localtime(saved.starts_at).date(), timezone.localtime(saved.ends_at).date())


    def test_legacy_multiday_booking_can_still_be_cancelled(self):
        entry = self.booking(starts_at=self.moment(self.day, 23),
                             ends_at=self.moment(self.day + timedelta(days=1), 1))
        set_booking_status(self.reporter, entry, 'cancelled')
        entry.refresh_from_db()
        self.assertEqual(entry.status, 'cancelled')
        self.assertEqual(entry.ends_at, self.moment(self.day + timedelta(days=1), 1))

    def test_producer_cannot_bypass_same_day_limit_with_a_booking_proposal(self):
        producer = User.objects.create_user(username='booking_producer')
        assign_role(producer, 'producer', Department.objects.get(slug='production'))
        proposal = Proposal(kind='booking', title='Предложение через полночь', studio=self.studio,
                            starts_at=self.moment(self.day, 23),
                            ends_at=self.moment(self.day + timedelta(days=1), 1))
        with self.assertRaises(ValidationError):
            save_proposal(producer, proposal)
        self.assertFalse(Proposal.objects.exists())
        self.assertFalse(StudioBooking.objects.exists())
