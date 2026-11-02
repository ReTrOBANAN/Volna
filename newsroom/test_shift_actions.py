"""Radio shift menus preserve server permissions and protected delete actions."""
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import BroadcastShift, Department
from .permissions import assign_role, setup_roles

User = get_user_model()


class ParsedActions(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        index = len(self.nodes)
        self.nodes.append({'tag': tag, 'attrs': dict(attrs), 'parent': self.stack[-1] if self.stack else None})
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(index)

    def handle_endtag(self, tag):
        while self.stack:
            if self.nodes[self.stack.pop()]['tag'] == tag:
                break

    def descendants(self, ancestor):
        result = []
        for index, node in enumerate(self.nodes):
            parent = node['parent']
            while parent is not None:
                if parent == ancestor:
                    result.append((index, node))
                    break
                parent = self.nodes[parent]['parent']
        return result


@override_settings(TIME_ZONE='Asia/Novosibirsk')
class ShiftActionMenuTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        department = Department.objects.get(slug='radio')
        cls.chief = User.objects.create_user(username='menu_chief', first_name='Главред радио')
        assign_role(cls.chief, 'radio_chief', department)
        cls.reporter = User.objects.create_user(username='menu_reporter', first_name='Корреспондент радио')
        assign_role(cls.reporter, 'radio_reporter', department)
        cls.day = date(2027, 5, 12)
        start = datetime.combine(cls.day, time(10), tzinfo=ZoneInfo('Asia/Novosibirsk'))
        cls.entry = BroadcastShift.objects.create(employee=cls.reporter, created_by=cls.chief, starts_at=start, ends_at=start + timedelta(hours=1))

    def setUp(self):
        self.client.force_login(self.chief)
        self.calendar_url = reverse('newsroom:shifts')

    def parse(self, response):
        parsed = ParsedActions()
        parsed.feed(response.content.decode())
        return parsed

    def test_manager_actions_are_inside_native_three_dot_menu_with_protected_post_form(self):
        response = self.client.get(self.calendar_url, {'date': self.day.isoformat()})
        self.assertEqual(response.status_code, 200)
        parsed = self.parse(response)
        details = [(index, node) for index, node in enumerate(parsed.nodes) if 'data-shift-actions' in node['attrs']]
        self.assertEqual(len(details), 1)
        details_index, details_node = details[0]
        self.assertEqual(details_node['tag'], 'details', 'native details remains usable without JavaScript')
        self.assertNotIn('open', details_node['attrs'])
        descendants = parsed.descendants(details_index)
        summaries = [(index, node) for index, node in descendants if node['tag'] == 'summary']
        self.assertEqual(len(summaries), 1)
        self.assertIn('Действия с эфиром', summaries[0][1]['attrs']['aria-label'])
        self.assertEqual(sum(node['tag'] == 'circle' for _, node in parsed.descendants(summaries[0][0])), 3)
        menus = [(index, node) for index, node in descendants if 'data-shift-actions-menu' in node['attrs']]
        self.assertEqual(len(menus), 1)
        menu_index, menu = menus[0]
        self.assertEqual(menu['attrs']['id'], f'shift-actions-{self.entry.pk}')
        self.assertNotIn('hidden', menu['attrs'])
        actions = parsed.descendants(menu_index)
        links = [node for _, node in actions if node['tag'] == 'a']
        monday = self.day - timedelta(days=self.day.weekday())
        self.assertEqual([node['attrs']['href'] for node in links], [f"{reverse('newsroom:shift_edit', args=[self.entry.pk])}?period={monday.isoformat()}"])
        forms = [(index, node) for index, node in actions if node['tag'] == 'form']
        self.assertEqual(len(forms), 1)
        form_index, form = forms[0]
        self.assertEqual(form['attrs']['method'].lower(), 'post')
        self.assertEqual(form['attrs']['action'], reverse('newsroom:shift_delete', args=[self.entry.pk]))
        self.assertEqual(form['attrs']['data-confirm'], 'Удалить эту запись из расписания?')
        csrf = [node for _, node in parsed.descendants(form_index) if node['tag'] == 'input' and node['attrs'].get('name') == 'csrfmiddlewaretoken']
        self.assertEqual(len(csrf), 1)
        self.assertTrue(csrf[0]['attrs']['value'])
        self.assertNotContains(response, 'class="shift-edit"')

    def test_read_only_reporter_sees_no_menu_and_cannot_post_actions_directly(self):
        self.client.force_login(self.reporter)
        response = self.client.get(self.calendar_url, {'date': self.day.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'data-shift-actions')
        self.assertNotContains(response, reverse('newsroom:shift_edit', args=[self.entry.pk]))
        self.assertNotContains(response, reverse('newsroom:shift_delete', args=[self.entry.pk]))
        self.assertEqual(self.client.post(reverse('newsroom:shift_delete', args=[self.entry.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse('newsroom:shift_edit', args=[self.entry.pk]), {}).status_code, 403)
        self.assertTrue(BroadcastShift.objects.filter(pk=self.entry.pk).exists())

    def test_rendered_delete_form_token_allows_actual_csrf_protected_delete(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.chief)
        response = client.get(self.calendar_url, {'date': self.day.isoformat()})
        parsed = self.parse(response)
        form_index = next(index for index, node in enumerate(parsed.nodes) if node['tag'] == 'form' and node['attrs'].get('action') == reverse('newsroom:shift_delete', args=[self.entry.pk]))
        token = next(node['attrs']['value'] for _, node in parsed.descendants(form_index) if node['tag'] == 'input' and node['attrs'].get('name') == 'csrfmiddlewaretoken')
        response = client.post(reverse('newsroom:shift_delete', args=[self.entry.pk]), {'csrfmiddlewaretoken': token}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(BroadcastShift.objects.filter(pk=self.entry.pk).exists())
        self.assertNotContains(response, 'Эфир удалён.')
