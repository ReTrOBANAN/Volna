"""Calendar emphasis follows the signed-in employee, not their management rights."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from html.parser import HTMLParser
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import BroadcastShift, Department, Event, Studio, StudioBooking
from .permissions import ROLE_DEPARTMENTS, assign_role, setup_roles

User = get_user_model()


class CalendarCards(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.depth = {'div': 0, 'a': 0, 'tr': 0}
        self.active = []
        self.records = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in self.depth:
            return
        self.depth[tag] += 1
        classes = set(attrs.get('class', '').split())
        if ((tag == 'div' and 'shift-entry' in classes)
                or (tag == 'a' and 'calendar-entry' in classes) or tag == 'tr'):
            record = {'tag': tag, 'depth': self.depth[tag], 'classes': classes,
                      'href': attrs.get('href'), 'links': [], 'text': ''}
            self.active.append(record)
            self.records.append(record)
        if tag == 'a':
            for record in self.active:
                record['links'].append(attrs.get('href'))

    def handle_endtag(self, tag):
        if tag not in self.depth:
            return
        self.active = [record for record in self.active
                       if not (record['tag'] == tag and record['depth'] == self.depth[tag])]
        self.depth[tag] -= 1

    def handle_data(self, data):
        for record in self.active:
            record['text'] += data

@override_settings(TIME_ZONE='Asia/Novosibirsk')
class PersonalCalendarTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        cls.users = {}
        roles = {
            'radio_a': 'radio_reporter', 'radio_b': 'radio_reporter', 'radio_chief': 'radio_chief',
            'engineer_a': 'engineer', 'engineer_b': 'engineer', 'director': 'director',
            'tv_reporter': 'tv_reporter', 'tv_chief': 'tv_chief', 'operator': 'operator',
            'photographer': 'photographer', 'editor': 'editor',
        }
        for key, role in roles.items():
            user = User.objects.create_user(username=f'personal_{key}', first_name=f'Сотрудник {key}')
            assign_role(user, role, Department.objects.get(slug=ROLE_DEPARTMENTS[role]))
            cls.users[key] = user
        cls.studio = Studio.objects.create(name='Студия личного расписания')

    def setUp(self):
        self.now = datetime(2027, 3, 12, 3, tzinfo=datetime_timezone.utc)
        clock = patch('django.utils.timezone.now', return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.date = '2027-03-12'

    def get_as(self, key, page, **query):
        self.client.force_login(self.users[key])
        response = self.client.get(reverse(f'newsroom:{page}'), {'date': self.date, **query})
        self.assertEqual(response.status_code, 200)
        return CalendarCards(response.content.decode())

    def assert_card(self, parsed, entry, expected, *, row=False):
        url = entry.get_absolute_url()
        if row:
            record = next(record for record in parsed.records if record['tag'] == 'tr' and url in record['links'])
        else:
            record = next(record for record in parsed.records if record['tag'] == 'a' and record['href'] == url)
        self.assertEqual('is-personal' in record['classes'], expected)

    def booking(self, title, hour, **kwargs):
        values = {'title': title, 'studio': self.studio, 'kind': 'video',
                  'created_by': self.users['radio_b'], 'responsible': self.users['radio_b'],
                  'starts_at': self.now + timedelta(hours=hour),
                  'ends_at': self.now + timedelta(hours=hour + 1)}
        values.update(kwargs)
        return StudioBooking.objects.create(**values)

    def test_shifts_emphasize_the_employee_even_in_an_archive_response_not_the_creator(self):
        start = datetime(2024, 1, 15, 3, tzinfo=datetime_timezone.utc)
        own = BroadcastShift.objects.create(employee=self.users['radio_a'], created_by=self.users['radio_chief'],
                                            starts_at=start, ends_at=start + timedelta(hours=1))
        other = BroadcastShift.objects.create(employee=self.users['radio_b'], created_by=self.users['radio_a'],
                                              starts_at=start + timedelta(hours=2), ends_at=start + timedelta(hours=3))
        original = list(BroadcastShift.objects.order_by('pk').values())
        for key, expected in [('radio_a', {own.employee_id}), ('radio_b', {other.employee_id}),
                              ('radio_chief', set()), ('director', set())]:
            with self.subTest(user=key):
                self.client.force_login(self.users[key])
                response = self.client.get(reverse('newsroom:shifts'), {'date': '2024-01-15'},
                                           HTTP_X_REQUESTED_WITH='XMLHttpRequest')
                self.assertEqual(response.status_code, 200)
                cards = [record for record in CalendarCards(response.content.decode()).records
                         if record['tag'] == 'div']
                self.assertEqual(len(cards), 2)
                for entry in [own, other]:
                    card = next(card for card in cards if entry.employee.get_full_name() in card['text'])
                    self.assertEqual('is-personal' in card['classes'], entry.employee_id in expected)
        self.assertEqual(list(BroadcastShift.objects.order_by('pk').values()), original)

    def test_studio_day_and_week_emphasize_creator_responsible_or_assigned_engineer(self):
        own = self.booking('Созданная мной', 1, created_by=self.users['radio_a'], responsible=self.users['radio_a'])
        assigned = self.booking('Работа инженера', 2, engineer=self.users['engineer_a'])
        responsible = self.booking('Историческая ответственность', 3, responsible=self.users['radio_a'])
        foreign = self.booking('Чужая запись', 4)
        entries = [own, assigned, responsible, foreign]
        original = list(StudioBooking.objects.order_by('pk').values())
        for mode in ['day', 'week']:
            for key, expected in [('radio_a', {own.pk, responsible.pk}), ('engineer_a', {assigned.pk}),
                                  ('radio_b', {assigned.pk, responsible.pk, foreign.pk}), ('director', set())]:
                with self.subTest(mode=mode, user=key):
                    parsed = self.get_as(key, 'bookings', view=mode)
                    for entry in entries:
                        self.assert_card(parsed, entry, entry.pk in expected)
        self.assertEqual(list(StudioBooking.objects.order_by('pk').values()), original)

    def test_releasing_and_reclaiming_changes_engineers_emphasis_but_keeps_the_creators(self):
        entry = self.booking('Смена инженера', 1, created_by=self.users['radio_a'],
                             responsible=self.users['radio_a'], engineer=self.users['engineer_a'])
        self.assert_card(self.get_as('engineer_a', 'bookings'), entry, True)
        self.assert_card(self.get_as('engineer_b', 'bookings'), entry, False)
        self.client.force_login(self.users['engineer_a'])
        self.assertRedirects(self.client.post(reverse('newsroom:booking_action', args=[entry.pk]),
                                              {'action': 'release'}), reverse('newsroom:bookings'))
        self.assert_card(self.get_as('engineer_a', 'bookings'), entry, False)
        self.assert_card(self.get_as('radio_a', 'bookings'), entry, True)
        self.client.force_login(self.users['engineer_b'])
        self.assertRedirects(self.client.post(reverse('newsroom:booking_action', args=[entry.pk]),
                                              {'action': 'claim'}), reverse('newsroom:bookings'))
        self.assert_card(self.get_as('engineer_a', 'bookings'), entry, False)
        self.assert_card(self.get_as('engineer_b', 'bookings'), entry, True)
        self.assert_card(self.get_as('radio_a', 'bookings'), entry, True)

    def test_tv_calendar_and_month_list_follow_actual_participation_for_each_role(self):
        cases = [('created_by', 'tv_reporter'), ('reporter', 'tv_reporter'), ('camera', 'operator'),
                 ('photographer', 'photographer'), ('editor', 'editor')]
        entries = []
        for index, (field, key) in enumerate(cases):
            values = {'title': f'Личный инфоповод {field}', 'created_by': self.users['tv_chief'],
                      'starts_at': self.now + timedelta(hours=index + 1),
                      'ends_at': self.now + timedelta(hours=index + 2), field: self.users[key]}
            entries.append((Event.objects.create(**values), key))
        unrelated = Event.objects.create(title='Инфоповод другой команды', created_by=self.users['tv_chief'],
                                         starts_at=self.now + timedelta(hours=7), ends_at=self.now + timedelta(hours=8))
        entries.append((unrelated, None))
        original = list(Event.objects.order_by('pk').values())
        for key in ['tv_reporter', 'operator', 'photographer', 'editor', 'director']:
            with self.subTest(user=key):
                parsed = self.get_as(key, 'events')
                for entry, participant in entries:
                    self.assert_card(parsed, entry, key == participant)
                    self.assert_card(parsed, entry, key == participant, row=True)
        self.assertEqual(list(Event.objects.order_by('pk').values()), original)
