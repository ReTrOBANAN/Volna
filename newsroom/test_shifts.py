"""User-facing radio shift date/range editing and access regression tests."""
import json
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from urllib.parse import urlencode
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from .models import BroadcastShift, Department
from .permissions import assign_role, setup_roles

User = get_user_model()
LOCAL_ZONE = ZoneInfo('Asia/Novosibirsk')


@override_settings(TIME_ZONE='Asia/Novosibirsk')
class ShiftDateRangeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        department = Department.objects.get(slug='radio')
        cls.chief = User.objects.create_user(username='shift_chief')
        assign_role(cls.chief, 'radio_chief', department)
        cls.reporter = User.objects.create_user(username='shift_reporter')
        assign_role(cls.reporter, 'radio_reporter', department)

    def setUp(self):
        self.client.force_login(self.chief)
        self.day = date(2027, 3, 12)
        self.create_url = reverse('newsroom:shift_create')
        self.calendar_url = reverse('newsroom:shifts')

    def local_datetime(self, day, hour, minute=0):
        return datetime.combine(day, time(hour, minute), tzinfo=LOCAL_ZONE)

    def shift(self, **kwargs):
        values = {
            'employee': self.reporter, 'created_by': self.chief,
            'starts_at': self.local_datetime(self.day, 10),
            'ends_at': self.local_datetime(self.day, 13),
        }
        values.update(kwargs)
        return BroadcastShift.objects.create(**values)

    def data(self, **kwargs):
        values = {
            'employee': str(self.reporter.pk), 'date': self.day.isoformat(),
            'start_time': '10:00', 'end_time': '13:00',
        }
        values.update(kwargs)
        return values

    def calendar_for(self, day):
        return f'{self.calendar_url}?date={day.isoformat()}'

    def test_create_form_uses_selected_date_and_native_time_range(self):
        response = self.client.get(self.create_url, {'date': self.day.isoformat()})
        self.assertEqual(response.status_code, 200)
        form = response.context['form']
        self.assertEqual(str(form['date'].value()), self.day.isoformat())
        self.assertEqual(form['start_time'].as_widget().count('10:00'), 1)
        self.assertEqual(form['end_time'].as_widget().count('13:00'), 1)
        self.assertContains(response, 'type="date"')
        self.assertContains(response, 'type="time"', count=2)
        self.assertNotContains(response, 'name="starts_at"')
        self.assertNotContains(response, 'name="ends_at"')
        self.assertNotContains(response, 'type="datetime-local"')
        self.assertNotContains(response, 'name="end_date"')
        self.assertNotContains(response, 'shift-range-summary')
        self.assertNotIn('end_date', form.fields)

    def test_create_saves_local_date_and_ignores_forged_old_datetime_fields(self):
        response = self.client.post(self.create_url, self.data(
            start_time='00:30', end_time='02:45',
            starts_at='2030-01-01T18:00', ends_at='2030-01-01T19:00',
        ))
        self.assertRedirects(response, self.calendar_for(self.day), fetch_redirect_response=False)
        entry = BroadcastShift.objects.get()
        self.assertEqual(entry.starts_at, self.local_datetime(self.day, 0, 30))
        self.assertEqual(entry.ends_at, self.local_datetime(self.day, 2, 45))
        self.assertEqual(timezone.localtime(entry.starts_at, LOCAL_ZONE).date(), self.day)
        self.assertEqual(entry.created_by, self.chief)

    def test_same_local_day_range_can_cross_utc_midnight(self):
        response = self.client.post(self.create_url, self.data(start_time='06:30', end_time='08:00'))
        self.assertRedirects(response, self.calendar_for(self.day), fetch_redirect_response=False)
        entry = BroadcastShift.objects.get()
        self.assertEqual(timezone.localtime(entry.starts_at, LOCAL_ZONE).date(), self.day)
        self.assertEqual(timezone.localtime(entry.ends_at, LOCAL_ZONE).date(), self.day)
        self.assertNotEqual(entry.starts_at.date(), entry.ends_at.date())

    def test_edit_form_uses_instance_local_date_even_with_other_query_date(self):
        entry = self.shift(starts_at=self.local_datetime(self.day, 0, 30), ends_at=self.local_datetime(self.day, 2, 45))
        response = self.client.get(reverse('newsroom:shift_edit', args=[entry.pk]), {'date': '2029-01-01'})
        self.assertEqual(response.status_code, 200)
        form = response.context['form']
        self.assertEqual(str(form['date'].value()), self.day.isoformat())
        self.assertIn('00:30', form['start_time'].as_widget())
        self.assertIn('02:45', form['end_time'].as_widget())
        self.assertNotIn('end_date', form.fields)
        self.assertEqual(response.context['cancel_url'], self.calendar_for(self.day))

    def test_existing_multiday_record_is_only_replaced_after_valid_same_day_edit(self):
        ending = self.day + timedelta(days=2)
        entry = self.shift(starts_at=self.local_datetime(self.day, 22), ends_at=self.local_datetime(ending, 6, 30))
        url = reverse('newsroom:shift_edit', args=[entry.pk])
        response = self.client.get(url)
        self.assertNotIn('end_date', response.context['form'].fields)
        entry.refresh_from_db()
        self.assertEqual(entry.ends_at, self.local_datetime(ending, 6, 30))
        response = self.client.post(url, self.data(start_time='22:00', end_time='06:30'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('end_time', response.context['form'].errors)
        entry.refresh_from_db()
        self.assertEqual(entry.ends_at, self.local_datetime(ending, 6, 30))
        response = self.client.post(url, self.data(start_time='22:00', end_time='23:30'))
        self.assertRedirects(response, self.calendar_for(self.day), fetch_redirect_response=False)
        entry.refresh_from_db()
        self.assertEqual(entry.ends_at, self.local_datetime(self.day, 23, 30))

    def test_valid_edit_updates_day_and_range_and_preserves_author(self):
        entry = self.shift(created_by=self.reporter)
        selected = self.day + timedelta(days=3)
        response = self.client.post(reverse('newsroom:shift_edit', args=[entry.pk]), self.data(
            date=selected.isoformat(), start_time='15:15', end_time='18:30',
        ))
        self.assertRedirects(response, self.calendar_for(selected), fetch_redirect_response=False)
        entry.refresh_from_db()
        self.assertEqual(entry.starts_at, self.local_datetime(selected, 15, 15))
        self.assertEqual(entry.ends_at, self.local_datetime(selected, 18, 30))
        self.assertEqual(entry.created_by, self.reporter)

    def test_same_day_equal_or_reversed_range_reports_end_time_error(self):
        for end_time in ['10:00', '09:59']:
            with self.subTest(end_time=end_time):
                response = self.client.post(self.create_url, self.data(end_time=end_time))
                self.assertEqual(response.status_code, 200)
                self.assertIn('end_time', response.context['form'].errors)
                self.assertEqual(response.context['form']['end_time'].value(), end_time)
                self.assertFalse(BroadcastShift.objects.exists())

    def test_invalid_edit_stays_bound_retains_input_and_does_not_overwrite_database(self):
        entry = self.shift()
        original = (entry.employee_id, entry.starts_at, entry.ends_at)
        url = reverse('newsroom:shift_edit', args=[entry.pk])
        for payload in [{}, self.data(date='not-a-date'), self.data(start_time='not-a-time'), self.data(start_time='17:00', end_time='14:00')]:
            with self.subTest(payload=payload):
                response = self.client.post(url, payload)
                self.assertEqual(response.status_code, 200)
                form = response.context['form']
                self.assertTrue(form.is_bound)
                self.assertTrue(form.errors)
                if 'start_time' in payload:
                    self.assertEqual(form['start_time'].value(), payload['start_time'])
                if 'date' in payload:
                    self.assertEqual(form['date'].value(), payload['date'])
                entry.refresh_from_db()
                self.assertEqual((entry.employee_id, entry.starts_at, entry.ends_at), original)

    def test_injected_legacy_end_date_cannot_enable_or_silently_schedule_overnight(self):
        ending = self.day + timedelta(days=1)
        for start_time, end_time in [('23:00', '01:30'), ('10:00', '13:00')]:
            with self.subTest(start_time=start_time, end_time=end_time):
                response = self.client.post(self.create_url, self.data(
                    start_time=start_time, end_time=end_time, end_date=ending.isoformat(),
                ))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['form'].non_field_errors())
                self.assertFalse(BroadcastShift.objects.exists())

    def test_radio_reporter_cannot_create_edit_or_delete_shifts(self):
        entry = self.shift()
        self.client.force_login(self.reporter)
        for url in [self.create_url, reverse('newsroom:shift_edit', args=[entry.pk])]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url, self.data()).status_code, 403)
        self.assertEqual(self.client.post(reverse('newsroom:shift_delete', args=[entry.pk])).status_code, 403)
        self.assertEqual(BroadcastShift.objects.count(), 1)
        self.assertEqual(self.client.get(self.calendar_url).status_code, 200)

    def test_inactive_or_nonradio_employee_is_rejected(self):
        outsider = User.objects.create_user(username='not_a_radio_employee')
        self.reporter.is_active = False
        self.reporter.save(update_fields=['is_active'])
        for employee_id in [self.reporter.pk, outsider.pk]:
            with self.subTest(employee_id=employee_id):
                response = self.client.post(self.create_url, self.data(employee=str(employee_id)))
                self.assertEqual(response.status_code, 200)
                self.assertIn('employee', response.context['form'].errors)
        self.assertFalse(BroadcastShift.objects.exists())

    def test_selected_second_week_day_retains_calendar_period_on_cancel_and_save(self):
        monday = date(2027, 3, 1)
        selected = monday + timedelta(days=10)
        query = urlencode({'date': selected.isoformat(), 'period': monday.isoformat()})
        url = f'{self.create_url}?{query}'
        response = self.client.get(url)
        self.assertEqual(str(response.context['form']['date'].value()), selected.isoformat())
        self.assertEqual(response.context['cancel_url'], self.calendar_for(monday))
        response = self.client.post(url, self.data(date=selected.isoformat()))
        self.assertRedirects(response, self.calendar_for(monday), fetch_redirect_response=False)
        entry = BroadcastShift.objects.get()
        self.assertEqual(timezone.localtime(entry.starts_at, LOCAL_ZONE).date(), selected)

    @patch('newsroom.views.timezone.localdate', return_value=date(2027, 3, 12))
    def test_invalid_or_outside_return_period_falls_back_to_saved_day(self, _localdate):
        for period in ['not-a-date', 'https://example.org/', '2027-02-01']:
            with self.subTest(period=period):
                url = f'{self.create_url}?{urlencode({"date": self.day.isoformat(), "period": period})}'
                response = self.client.post(url, self.data())
                self.assertRedirects(response, self.calendar_for(self.day), fetch_redirect_response=False)


    def test_calendar_add_links_remain_on_each_day_and_header_add_is_removed(self):
        response = self.client.get(self.calendar_url, {'date': self.day.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="shift-add-link"', count=14)

        class HeaderLinks(HTMLParser):
            def __init__(self):
                super().__init__()
                self.divs = []
                self.links = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'div':
                    self.divs.append(any(self.divs) or 'page-heading' in attrs.get('class', '').split())
                elif tag == 'a' and any(self.divs):
                    self.links.append(attrs.get('href', ''))

            def handle_endtag(self, tag):
                if tag == 'div' and self.divs:
                    self.divs.pop()

        parser = HeaderLinks()
        parser.feed(response.content.decode())
        self.assertFalse(any(self.create_url in link for link in parser.links))

    def test_calendar_date_navigation_renders_the_selected_past_period_without_a_submit_button(self):
        selected = date(2024, 2, 29)
        entry = self.shift(starts_at=self.local_datetime(selected, 10), ends_at=self.local_datetime(selected, 13))
        for user in [self.chief, self.reporter]:
            with self.subTest(role=user.employee.role):
                self.client.force_login(user)
                response = self.client.get(self.calendar_url, {'date': selected.isoformat()})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['selected_date'], selected)
                self.assertEqual(response.context['start'], date(2024, 2, 26))
                self.assertEqual(response.context['end'], date(2024, 3, 10))
                day = next(item for item in response.context['days'] if item['date'] == selected)
                self.assertEqual([shift.pk for shift in day['shifts']], [entry.pk])
                form = response.content.decode().split('class="shift-calendar-jump"', 1)[1].split('</form>', 1)[0]
                self.assertIn('id="shift-calendar-date"', form)
                self.assertIn('value="2024-02-29"', form)
                self.assertNotIn('<button', form)
                self.assertContains(response, 'js/shift_calendar.js')

    def test_create_preview_lists_only_selected_local_day_in_chronological_order(self):
        late = self.shift(starts_at=self.local_datetime(self.day, 17), ends_at=self.local_datetime(self.day, 18))
        early = self.shift(starts_at=self.local_datetime(self.day, 0, 30), ends_at=self.local_datetime(self.day, 2))
        self.shift(starts_at=self.local_datetime(self.day - timedelta(days=1), 23), ends_at=self.local_datetime(self.day - timedelta(days=1), 23, 30))
        self.shift(starts_at=self.local_datetime(self.day + timedelta(days=1), 8), ends_at=self.local_datetime(self.day + timedelta(days=1), 9))
        response = self.client.get(self.create_url, {'date': self.day.isoformat()})
        self.assertEqual(response.context['schedule_date'], self.day)
        self.assertIsNone(response.context['editing_shift_id'])
        listing = response.context['day_schedule']
        self.assertEqual([row['id'] for row in listing], [early.pk, late.pk])
        self.assertEqual(listing[0]['employee_id'], self.reporter.pk)
        self.assertEqual(listing[0]['employee'], self.reporter.username)
        self.assertEqual((listing[0]['start_time'], listing[0]['end_time']), ('00:30', '02:00'))
        self.assertContains(response, 'data-shift-day-schedule')
        self.assertContains(response, 'id="shift-day-schedule-data"')
        self.assertContains(response, reverse('newsroom:shift_day_schedule'))

    def test_edit_preview_excludes_original_record(self):
        entry = self.shift()
        other = self.shift(starts_at=self.local_datetime(self.day, 15), ends_at=self.local_datetime(self.day, 16))
        response = self.client.get(reverse('newsroom:shift_edit', args=[entry.pk]))
        self.assertEqual(response.context['editing_shift_id'], entry.pk)
        self.assertEqual([row['id'] for row in response.context['day_schedule']], [other.pk])
        response = self.client.get(reverse('newsroom:shift_day_schedule'), {'date': self.day.isoformat(), 'exclude': entry.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.json()['shifts']], [other.pk])

    def test_preview_endpoint_refreshes_for_changed_date_and_retains_inactive_employee(self):
        self.shift()
        changed_day = self.day + timedelta(days=1)
        entry = self.shift(starts_at=self.local_datetime(changed_day, 8, 15), ends_at=self.local_datetime(changed_day, 10))
        self.reporter.is_active = False
        self.reporter.save(update_fields=['is_active'])
        response = self.client.get(reverse('newsroom:shift_day_schedule'), {'date': changed_day.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['date'], changed_day.isoformat())
        self.assertEqual(response.json()['shifts'], [{
            'id': entry.pk, 'employee_id': self.reporter.pk, 'employee': self.reporter.username,
            'start_time': '08:15', 'end_time': '10:00', 'notes': '',
        }])

    def test_invalid_post_preview_uses_submitted_valid_date_without_writing(self):
        changed_day = self.day + timedelta(days=3)
        entry = self.shift(starts_at=self.local_datetime(changed_day, 11), ends_at=self.local_datetime(changed_day, 12))
        response = self.client.post(f'{self.create_url}?date={self.day.isoformat()}', self.data(
            date=changed_day.isoformat(), start_time='16:00', end_time='13:00',
        ))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['schedule_date'], changed_day)
        self.assertEqual([row['id'] for row in response.context['day_schedule']], [entry.pk])
        self.assertEqual(response.context['form']['start_time'].value(), '16:00')
        self.assertEqual(BroadcastShift.objects.count(), 1)
        response = self.client.post(f'{self.create_url}?date={self.day.isoformat()}', self.data(date='invalid-date'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['schedule_date'], self.day)
        self.assertEqual(response.context['form']['date'].value(), 'invalid-date')

    def test_schedule_endpoint_requires_view_permission_and_is_read_only(self):
        url = reverse('newsroom:shift_day_schedule')
        self.client.logout()
        response = self.client.get(url, {'date': self.day.isoformat()})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('newsroom:login'), response.url)
        self.client.force_login(self.reporter)
        self.assertEqual(self.client.get(url, {'date': self.day.isoformat()}).status_code, 200)
        self.assertEqual(self.client.post(url, {'date': self.day.isoformat()}).status_code, 405)
        outsider = User.objects.create_user(username='preview_engineer')
        assign_role(outsider, 'engineer', Department.objects.get(slug='tv'))
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(url, {'date': self.day.isoformat()}).status_code, 403)

    def test_schedule_endpoint_rejects_invalid_dates_or_exclusion(self):
        url = reverse('newsroom:shift_day_schedule')
        for query in [{}, {'date': ''}, {'date': 'not-a-date'}, {'date': '2027-02-30'}, {'date': self.day.isoformat(), 'exclude': 'not-an-id'}]:
            with self.subTest(query=query):
                self.assertEqual(self.client.get(url, query).status_code, 400)

    def test_preview_serializes_employee_names_without_executable_markup(self):
        self.reporter.first_name = '</script><script>alert(1)</script>'
        self.reporter.save(update_fields=['first_name'])
        self.shift()
        response = self.client.get(self.create_url, {'date': self.day.isoformat()})
        self.assertEqual(response.context['day_schedule'][0]['employee'], self.reporter.first_name)
        self.assertContains(response, 'id="shift-day-schedule-data"')
        self.assertNotContains(response, '<script>alert(1)</script>')


    def test_optional_note_persists_updates_and_can_be_cleared(self):
        note = 'Интервью с гостем\nПодготовить вопросы к 10:00.'
        response = self.client.post(self.create_url, self.data(notes=note))
        self.assertRedirects(response, self.calendar_for(self.day), fetch_redirect_response=False)
        entry = BroadcastShift.objects.get()
        self.assertEqual(entry.notes, note)
        url = reverse('newsroom:shift_edit', args=[entry.pk])
        response = self.client.get(url)
        self.assertEqual(response.context['form']['notes'].value(), note)
        updated = 'Уточнённый план\nВключить запись интервью.'
        response = self.client.post(url, self.data(notes=updated))
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.notes, updated)
        response = self.client.post(url, self.data(notes=''))
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.notes, '')

    def test_omitted_note_remains_optional_for_existing_clients(self):
        response = self.client.get(self.create_url, {'date': self.day.isoformat()})
        field = response.context['form'].fields['notes']
        self.assertFalse(field.required)
        self.assertEqual(field.label, 'Примечание')
        self.assertEqual(str(field.widget.attrs.get('rows')), '3')
        self.assertContains(response, 'name="notes"')
        response = self.client.post(self.create_url, self.data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(BroadcastShift.objects.get().notes, '')

    def test_invalid_edit_retains_entered_note_without_overwriting_saved_note(self):
        entry = self.shift(notes='Сохранённое примечание')
        replacement = 'Новый план\nНе потерять после ошибки времени.'
        response = self.client.post(reverse('newsroom:shift_edit', args=[entry.pk]), self.data(
            start_time='16:00', end_time='13:00', notes=replacement,
        ))
        self.assertEqual(response.status_code, 200)
        self.assertIn('end_time', response.context['form'].errors)
        self.assertEqual(response.context['form']['notes'].value(), replacement)
        entry.refresh_from_db()
        self.assertEqual(entry.notes, 'Сохранённое примечание')
        self.assertEqual(entry.starts_at.astimezone(LOCAL_ZONE).hour, 10)

    def test_note_markup_is_literal_in_calendar_preview_and_json_script(self):
        note = '<img src=x onerror=alert(7)>\n</script><script>alert("shift-note")</script>'
        entry = self.shift(notes=note)
        self.client.force_login(self.reporter)
        response = self.client.get(self.calendar_url, {'date': self.day.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, escape(note))
        self.assertNotContains(response, '<img src=x onerror=alert(7)>')
        self.assertNotContains(response, '<script>alert("shift-note")</script>')
        response = self.client.get(reverse('newsroom:shift_day_schedule'), {'date': self.day.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Type'].startswith('application/json'))
        self.assertEqual(response.json()['shifts'][0]['notes'], note)
        self.client.force_login(self.chief)
        response = self.client.get(self.create_url, {'date': self.day.isoformat()})
        self.assertEqual(response.context['day_schedule'][0]['notes'], note)
        self.assertContains(response, escape(note))
        self.assertNotContains(response, '<script>alert("shift-note")</script>')

        class EmbeddedSchedule(HTMLParser):
            def __init__(self):
                super().__init__()
                self.inside = False
                self.parts = []

            def handle_starttag(self, tag, attrs):
                if tag == 'script' and dict(attrs).get('id') == 'shift-day-schedule-data':
                    self.inside = True

            def handle_data(self, data):
                if self.inside:
                    self.parts.append(data)

            def handle_endtag(self, tag):
                if tag == 'script':
                    self.inside = False

        parser = EmbeddedSchedule()
        parser.feed(response.content.decode())
        payload = json.loads(''.join(parser.parts))
        self.assertEqual(payload[0]['id'], entry.pk)
        self.assertEqual(payload[0]['notes'], note)

    def test_note_limit_accepts_2000_characters_and_rejects_longer_edit(self):
        response = self.client.post(self.create_url, self.data(notes='я' * 2000))
        self.assertEqual(response.status_code, 302)
        entry = BroadcastShift.objects.get()
        self.assertEqual(len(entry.notes), 2000)
        response = self.client.post(reverse('newsroom:shift_edit', args=[entry.pk]), self.data(notes='я' * 2001))
        self.assertEqual(response.status_code, 200)
        self.assertIn('notes', response.context['form'].errors)
        entry.refresh_from_db()
        self.assertEqual(entry.notes, 'я' * 2000)

    def test_read_only_reporter_cannot_change_saved_note(self):
        entry = self.shift(notes='План редактора')
        self.client.force_login(self.reporter)
        response = self.client.post(reverse('newsroom:shift_edit', args=[entry.pk]), self.data(notes='Подмена'))
        self.assertEqual(response.status_code, 403)
        entry.refresh_from_db()
        self.assertEqual(entry.notes, 'План редактора')
