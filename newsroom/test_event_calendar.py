"""Event calendar navigation, day-based creation and modal entry keep workflow permissions."""
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from html.parser import HTMLParser
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from .models import Department, Event, EventAssignment, EventStatusHistory
from .permissions import ROLE_DEPARTMENTS, assign_role, setup_roles

User = get_user_model()


class EventCalendarHTML(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.nodes = []
        self.forms = []
        self.links = []
        self.form = None
        self.link = None
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.nodes.append((tag, attrs))
        if tag == 'form':
            self.form = {'attrs': attrs, 'fields': [], 'buttons': []}
            self.forms.append(self.form)
        if self.form is not None:
            if tag in ('input', 'select'):
                self.form['fields'].append(attrs)
            elif tag == 'button':
                self.form['buttons'].append(attrs)
        if tag == 'a':
            self.link = {'attrs': attrs, 'text': ''}
            self.links.append(self.link)

    def handle_endtag(self, tag):
        if tag == 'form':
            self.form = None
        elif tag == 'a':
            self.link = None

    def handle_data(self, data):
        if self.link is not None:
            self.link['text'] += data

    def field(self, name):
        return next(attrs for tag, attrs in self.nodes if tag == 'input' and attrs.get('name') == name)


@override_settings(TIME_ZONE='Asia/Novosibirsk')
class EventCalendarTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        cls.users = {}
        for role in ['tv_chief', 'tv_reporter', 'operator', 'radio_reporter']:
            user = User.objects.create_user(username=f'event_calendar_{role}', first_name=role)
            assign_role(user, role, Department.objects.get(slug=ROLE_DEPARTMENTS[role]))
            cls.users[role] = user
        cls.other_reporter = User.objects.create_user(username='event_calendar_other_reporter')
        assign_role(cls.other_reporter, 'tv_reporter', Department.objects.get(slug='tv'))

    def setUp(self):
        self.now = datetime(2027, 3, 12, 3, tzinfo=datetime_timezone.utc)
        clock = patch('django.utils.timezone.now', return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.client.force_login(self.users['tv_chief'])
        self.url = reverse('newsroom:events')
        self.create_url = reverse('newsroom:event_create')

    def event(self, **kwargs):
        values = {
            'title': 'Инфоповод календаря', 'created_by': self.users['tv_chief'],
            'starts_at': self.now - timedelta(hours=2), 'ends_at': self.now - timedelta(hours=1),
            'reporter': self.users['tv_reporter'],
        }
        values.update(kwargs)
        return Event.objects.create(**values)

    def payload(self, **kwargs):
        values = {'title': 'Сохранённый инфоповод', 'description': 'Описание', 'location': 'Редакция',
                  'starts_at': '2027-03-12T10:00', 'ends_at': '2027-03-12T11:00',
                  'reporter': self.users['tv_reporter'].pk}
        values.update(kwargs)
        return values

    def test_archive_date_and_status_form_update_calendar_and_list_together(self):
        start = timezone.make_aware(datetime(2024, 2, 29, 10))
        selected = self.event(title='Високосный день', starts_at=start, ends_at=start + timedelta(hours=1))
        self.event(title='Другой этап', status='shot', starts_at=start, ends_at=start + timedelta(hours=1))
        response = self.client.get(self.url, {'date': '2024-02-29', 'status': 'planned'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_date'], date(2024, 2, 29))
        self.assertEqual(response.context['selected_status'], 'planned')
        self.assertEqual([entry.pk for entry in response.context['events']], [selected.pk])
        days = [day for week in response.context['weeks'] for day in week]
        self.assertEqual([day['date'] for day in days if day['is_selected']], [date(2024, 2, 29)])
        parsed = EventCalendarHTML(response)
        form = next(form for form in parsed.forms
                    if any(field.get('id') == 'event-calendar-date' for field in form['fields']))
        self.assertEqual(form['attrs'].get('method', '').lower(), 'get')
        self.assertEqual(form['attrs']['action'], self.url)
        self.assertTrue(any(field.get('id') == 'status-filter' for field in form['fields']))
        self.assertFalse(any(button.get('type') == 'submit' for button in form['buttons']))
        field = parsed.field('date')
        self.assertEqual((field['value'], field['min'], field['max']), ('2024-02-29', '1970-01-01', '2200-12-31'))
        for label, month in [('Предыдущий месяц', '2024-01'), ('Следующий месяц', '2024-03')]:
            link = next(link for link in parsed.links if link['attrs'].get('aria-label') == label)
            query = parse_qs(urlsplit(link['attrs']['href']).query)
            self.assertEqual(query['status'], ['planned'])
            self.assertTrue(query['date'][0].startswith(month))
        current = next(link for link in parsed.links if link['text'].strip() == 'Текущий месяц')
        self.assertEqual(parse_qs(urlsplit(current['attrs']['href']).query).get('status'), ['planned'])
        self.assertContains(response, 'js/event_calendar.js')
        self.assertContains(response, 'data-event-calendar')
        self.assertContains(response, 'data-event-list')

    def test_invalid_date_and_status_fall_back_without_losing_today_selection(self):
        for value in ['not-a-date', '1969-12-31', '2201-01-01', '2024-02-30']:
            with self.subTest(date=value):
                response = self.client.get(self.url, {'date': value, 'status': 'not-a-status'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['selected_date'], date(2027, 3, 12))
                self.assertEqual(response.context['selected_status'], '')
                days = [day for week in response.context['weeks'] for day in week]
                self.assertEqual([day['date'] for day in days if day['is_today']], [date(2027, 3, 12)])

    def test_creation_links_are_inside_each_day_and_respect_create_permissions(self):
        for role, allowed in [('tv_chief', True), ('tv_reporter', True), ('operator', False)]:
            with self.subTest(role=role):
                self.client.force_login(self.users[role])
                response = self.client.get(self.url, {'date': '2027-03-12'})
                parsed = EventCalendarHTML(response)
                links = [link for link in parsed.links
                         if 'event-add-link' in link['attrs'].get('class', '').split()]
                creation_links = [link for link in parsed.links
                                  if urlsplit(link['attrs'].get('href', '')).path == self.create_url]
                days = [day for week in response.context['weeks'] for day in week]
                self.assertEqual(len(links), len(days) if allowed else 0)
                self.assertEqual(len(creation_links), len(links), 'creation stays inside the day cells')
                self.assertFalse(any('data-event-create' in link['attrs'] for link in parsed.links))
                if allowed:
                    actual = {parse_qs(urlsplit(link['attrs']['href']).query)['date'][0] for link in links}
                    self.assertEqual(actual, {day['date'].isoformat() for day in days})
                    self.assertTrue(all(urlsplit(link['attrs']['href']).path == self.create_url for link in links))
        self.assertEqual(self.client.get(self.create_url).status_code, 403)
        self.assertEqual(self.client.post(self.create_url, self.payload()).status_code, 403)
        self.client.force_login(self.users['radio_reporter'])
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_personal_cards_and_table_links_open_the_modal_and_nullable_team_renders(self):
        own = self.event(title='<script>alert("calendar")</script>')
        foreign = self.event(title='Чужой инфоповод', reporter=self.other_reporter)
        self.client.force_login(self.users['tv_reporter'])
        response = self.client.get(self.url)
        parsed = EventCalendarHTML(response)
        for entry, personal in [(own, True), (foreign, False)]:
            links = [link for link in parsed.links if link['attrs'].get('href') == entry.get_absolute_url()]
            self.assertEqual(len(links), 2)
            self.assertTrue(all('data-event-preview' in link['attrs'] for link in links))
            card = next(link for link in links if 'calendar-entry' in link['attrs'].get('class', '').split())
            self.assertEqual('is-personal' in card['attrs'].get('class', '').split(), personal)
        self.assertNotContains(response, own.title)
        self.assertContains(response, escape(own.title))
        self.assertTrue(any(tag == 'dialog' and 'data-event-dialog' in attrs for tag, attrs in parsed.nodes))
        self.assertTrue(any(tag == 'button' and 'data-dialog-close' in attrs for tag, attrs in parsed.nodes))
        empty_team = self.event(reporter=None)
        self.client.force_login(self.users['tv_chief'])
        detail = self.client.get(empty_team.get_absolute_url())
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'Не назначен', count=4)

    def test_day_link_prefills_local_time_and_invalid_dates_use_today(self):
        response = self.client.get(self.create_url, {'date': '2024-02-29'})
        parsed = EventCalendarHTML(response)
        self.assertEqual(parsed.field('starts_at')['value'], '2024-02-29T10:00')
        self.assertEqual(parsed.field('ends_at')['value'], '2024-02-29T11:00')
        for value in ['bad-date', '1969-01-01', '2201-01-01', '2024-02-30']:
            with self.subTest(date=value):
                response = self.client.get(self.create_url, {'date': value})
                parsed = EventCalendarHTML(response)
                self.assertEqual(parsed.field('starts_at')['value'], '2027-03-12T10:00')
                self.assertEqual(parsed.field('ends_at')['value'], '2027-03-12T11:00')

    def test_edit_and_invalid_post_keep_existing_or_entered_dates_instead_of_query_date(self):
        entry = self.event()
        response = self.client.get(reverse('newsroom:event_edit', args=[entry.pk]), {'date': '2024-02-29'})
        parsed = EventCalendarHTML(response)
        self.assertEqual(parsed.field('starts_at')['value'], '2027-03-12T08:00')
        self.assertEqual(parsed.field('ends_at')['value'], '2027-03-12T09:00')
        response = self.client.post(self.create_url + '?date=2024-02-29', self.payload(
            title='', starts_at='2028-01-05T14:15', ends_at='2028-01-05T13:00'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        parsed = EventCalendarHTML(response)
        self.assertEqual(parsed.field('starts_at')['value'], '2028-01-05T14:15')
        self.assertEqual(parsed.field('ends_at')['value'], '2028-01-05T13:00')
        self.assertEqual(Event.objects.count(), 1)

    def test_successful_create_and_edit_return_to_the_event_calendar(self):
        response = self.client.post(self.create_url, self.payload())
        self.assertRedirects(response, self.url)
        entry = Event.objects.get()
        self.assertEqual(entry.created_by, self.users['tv_chief'])
        response = self.client.post(reverse('newsroom:event_edit', args=[entry.pk]), self.payload(title='Уточнённый инфоповод'))
        self.assertRedirects(response, self.url)
        entry.refresh_from_db()
        self.assertEqual(entry.title, 'Уточнённый инфоповод')

    def test_stage_and_assignment_actions_return_to_calendar_with_permissions_and_errors_intact(self):
        entry = self.event(camera=self.users['operator'])
        url = reverse('newsroom:event_action', args=[entry.pk])
        self.client.force_login(self.users['tv_reporter'])
        self.assertRedirects(self.client.post(url, {'action': 'shot'}), self.url)
        entry.refresh_from_db()
        self.assertEqual(entry.status, 'shot')
        rejected = self.client.post(url, {'action': 'shot'}, follow=True)
        self.assertRedirects(rejected, self.url)
        self.assertContains(rejected, 'message-error')
        self.assertEqual(EventStatusHistory.objects.filter(event=entry, new_status='shot').count(), 1)
        self.client.force_login(self.users['operator'])
        self.assertEqual(self.client.post(url, {'action': 'shot'}).status_code, 403)
        assignment = EventAssignment.objects.create(event=entry, employee=self.users['operator'], job='camera')
        self.assertRedirects(self.client.post(url, {'action': 'assignment', 'status': 'working'}), self.url)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, 'working')
