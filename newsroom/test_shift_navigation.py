"""Historical radio schedules are reachable by date and calendar-based shortcuts."""
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from unittest.mock import patch
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import BroadcastShift, Department
from .permissions import assign_role, setup_roles

User = get_user_model()


class ArchiveNavigation(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.links = []
        self.labels = []
        self.selected_days = []
        self.form = None
        self.link = None
        self.label = None
        self.button = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.form = {'attrs': attrs, 'inputs': [], 'buttons': []}
            self.forms.append(self.form)
        elif tag == 'input' and self.form is not None:
            self.form['inputs'].append(attrs)
        elif tag == 'button' and self.form is not None:
            self.button = {'attrs': attrs, 'text': ''}
            self.form['buttons'].append(self.button)
        if tag == 'a':
            self.link = {'attrs': attrs, 'text': ''}
            self.links.append(self.link)
        if tag == 'label':
            self.label = {'attrs': attrs, 'text': ''}
            self.labels.append(self.label)
        if tag == 'div' and {'calendar-day', 'is-selected'} <= set(attrs.get('class', '').split()):
            self.selected_days.append(attrs)

    def handle_data(self, data):
        for current in [self.link, self.label, self.button]:
            if current is not None:
                current['text'] += data

    def handle_endtag(self, tag):
        if tag == 'form':
            self.form = None
        elif tag == 'button':
            self.button = None
        elif tag == 'a':
            self.link = None
        elif tag == 'label':
            self.label = None


@override_settings(TIME_ZONE='Asia/Novosibirsk')
class ShiftArchiveNavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        cls.chief = User.objects.create_user(username='archive_chief')
        assign_role(cls.chief, 'radio_chief', Department.objects.get(slug='radio'))
        cls.reporter = User.objects.create_user(username='archive_reporter')
        assign_role(cls.reporter, 'radio_reporter', Department.objects.get(slug='radio'))
        cls.engineer = User.objects.create_user(username='archive_engineer')
        assign_role(cls.engineer, 'engineer', Department.objects.get(slug='tv'))

    def setUp(self):
        self.client.force_login(self.chief)
        self.url = reverse('newsroom:shifts')
        self.today = date(2026, 11, 3)
        frozen = patch('newsroom.views.timezone.localdate', return_value=self.today)
        self.localdate = frozen.start()
        self.addCleanup(frozen.stop)

    def shift(self, day, note, hour=10, minute=0):
        start = datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo('Asia/Novosibirsk'))
        return BroadcastShift.objects.create(employee=self.reporter, created_by=self.chief,
                                             starts_at=start, ends_at=start + timedelta(minutes=30), notes=note)

    def parse(self, response):
        parsed = ArchiveNavigation()
        parsed.feed(response.content.decode())
        return parsed

    def destination(self, parsed, *, text=None, aria_label=None):
        link = next(link for link in parsed.links if
                    (text is not None and link['text'].strip() == text) or
                    (aria_label is not None and link['attrs'].get('aria-label') == aria_label))
        target = urljoin(self.url, link['attrs']['href'])
        self.assertEqual(urlsplit(target).path, self.url)
        return target

    def test_half_year_and_year_links_open_actual_old_records_without_current_records(self):
        past = [('Полгода назад', date(2026, 5, 3), 'Архив за полгода'),
                ('Год назад', date(2025, 11, 3), 'Архив за прошлый год')]
        self.shift(self.today, 'Только текущий период')
        for _, day, note in past:
            self.shift(day, note)
        landing = self.client.get(self.url)
        parsed = self.parse(landing)
        for label, day, note in past:
            with self.subTest(label=label):
                target = self.destination(parsed, text=label)
                self.assertEqual(parse_qs(urlsplit(target).query)['date'], [day.isoformat()])
                response = self.client.get(target)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['selected_date'], day)
                self.assertContains(response, note)
                self.assertNotContains(response, 'Только текущий период')
                selected = [item['date'] for item in response.context['days'] if item['is_selected']]
                self.assertEqual(selected, [day])

    def test_date_form_displays_selected_date_and_highlights_exactly_one_day(self):
        selected = date(2001, 7, 19)
        response = self.client.get(self.url, {'date': selected.isoformat()})
        self.assertEqual(response.status_code, 200)
        parsed = self.parse(response)
        form = next(form for form in parsed.forms if any(field.get('id') == 'shift-calendar-date' for field in form['inputs']))
        self.assertEqual(form['attrs']['method'].lower(), 'get')
        self.assertEqual(form['attrs']['action'], self.url)
        field = next(field for field in form['inputs'] if field.get('id') == 'shift-calendar-date')
        self.assertEqual(field['name'], 'date')
        self.assertEqual(field['type'], 'date')
        self.assertEqual(field['value'], selected.isoformat())
        self.assertEqual(field['min'], '1970-01-01')
        self.assertEqual(field['max'], '2200-12-31')
        self.assertIn('required', field)
        label = next(label for label in parsed.labels if label['attrs'].get('for') == 'shift-calendar-date')
        self.assertEqual(label['text'].strip(), 'Перейти к дате')
        self.assertTrue(any(button['text'].strip() == 'Показать' for button in form['buttons']))
        self.assertEqual(len(parsed.selected_days), 1)
        self.assertEqual([day['date'] for day in response.context['days'] if day['is_selected']], [selected])

    def test_empty_archive_stays_empty_and_previous_next_move_within_the_old_calendar(self):
        selected = date(1999, 10, 21)
        self.shift(self.today, 'Текущий эфир вне архива')
        response = self.client.get(self.url, {'date': selected.isoformat()})
        self.assertEqual(response.status_code, 200)
        start = selected - timedelta(days=selected.weekday())
        self.assertEqual(response.context['start'], start)
        self.assertEqual(len(response.context['days']), 14)
        self.assertTrue(all(not day['shifts'] for day in response.context['days']))
        self.assertContains(response, 'Нет эфиров', count=14)
        parsed = self.parse(response)
        for aria_label, expected in [('Предыдущие две недели', start - timedelta(days=14)),
                                     ('Следующие две недели', start + timedelta(days=14))]:
            with self.subTest(direction=aria_label):
                page = self.client.get(self.destination(parsed, aria_label=aria_label))
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page.context['start'], expected)
                self.assertNotContains(page, 'Текущий эфир вне архива')

    def test_archive_navigation_keeps_existing_view_permissions(self):
        self.client.force_login(self.reporter)
        response = self.client.get(self.url, {'date': '2025-11-03'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'shift-calendar-date')
        self.assertNotContains(response, 'class="shift-add-link"')
        self.client.force_login(self.engineer)
        self.assertEqual(self.client.get(self.url, {'date': '2025-11-03'}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.url, {'date': '2025-11-03'}).status_code, 302)

    def test_archive_range_uses_local_midnight_and_excludes_both_outside_boundaries(self):
        monday = date(2025, 11, 3)
        first = self.shift(monday, 'Локальный понедельник', hour=0, minute=30)
        last = self.shift(monday + timedelta(days=13), 'Последний день периода', hour=23)
        self.shift(monday - timedelta(days=1), 'До начала периода', hour=23)
        self.shift(monday + timedelta(days=14), 'После конца периода', hour=0, minute=30)
        response = self.client.get(self.url, {'date': monday.isoformat()})
        self.assertEqual(response.status_code, 200)
        days = response.context['days']
        self.assertEqual([entry.pk for entry in days[0]['shifts']], [first.pk])
        self.assertEqual([entry.pk for entry in days[-1]['shifts']], [last.pk])
        self.assertEqual({entry.pk for day in days for entry in day['shifts']}, {first.pk, last.pk})
        self.assertContains(response, '00:30')
        self.assertNotContains(response, 'До начала периода')
        self.assertNotContains(response, 'После конца периода')

    def test_shortcuts_use_calendar_months_with_leap_day_clamping_and_year_rollover(self):
        cases = [
            (date(2024, 8, 31), date(2024, 2, 29), date(2023, 8, 31)),
            (date(2025, 8, 31), date(2025, 2, 28), date(2024, 8, 31)),
            (date(2024, 2, 29), date(2023, 8, 29), date(2023, 2, 28)),
            (date(2026, 1, 31), date(2025, 7, 31), date(2025, 1, 31)),
        ]
        for today, half_year, year in cases:
            with self.subTest(today=today):
                self.localdate.return_value = today
                response = self.client.get(self.url, {'date': '2001-07-19'})
                self.assertEqual(response.status_code, 200)
                dates = {item['label']: item['date'] for item in response.context['archive_shortcuts']}
                self.assertEqual(dates, {'Полгода назад': half_year, 'Год назад': year})
                parsed = self.parse(response)
                for label, expected in dates.items():
                    target = self.destination(parsed, text=label)
                    self.assertEqual(parse_qs(urlsplit(target).query)['date'], [expected.isoformat()])

    def test_date_bounds_and_invalid_input_fall_back_to_the_frozen_local_day(self):
        for selected in ['1969-12-31', '2201-01-01', '2026-02-30', 'not-a-date', '']:
            with self.subTest(selected=selected):
                response = self.client.get(self.url, {'date': selected})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['selected_date'], self.today)
        for selected in [date(1970, 1, 1), date(2200, 12, 31)]:
            with self.subTest(boundary=selected):
                response = self.client.get(self.url, {'date': selected.isoformat()})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['selected_date'], selected)
