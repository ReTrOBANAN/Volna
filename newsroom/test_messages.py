"""Successful actions stay quiet; redirected errors remain visible and safe inline."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from html.parser import HTMLParser

from unittest.mock import patch

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from .models import BroadcastShift, Department, Studio, StudioBooking
from .permissions import assign_role, setup_roles

User = get_user_model()


class InlineAlerts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_main = False
        self.alerts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'main':
            self.in_main = True
        if attrs.get('role') == 'alert':
            self.alerts.append((attrs, self.in_main))

    def handle_endtag(self, tag):
        if tag == 'main':
            self.in_main = False


class InlineMessageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        cls.user = User.objects.create_user(username='message_radio_chief', first_name='Главред радио')
        assign_role(cls.user, 'radio_chief', Department.objects.get(slug='radio'))

    def setUp(self):
        frozen = patch('django.utils.timezone.now', return_value=datetime(2027, 3, 1, 3, tzinfo=datetime_timezone.utc))
        frozen.start()
        self.addCleanup(frozen.stop)
        self.client.force_login(self.user)

    def assertNoSideMessages(self, response):
        self.assertNotContains(response, 'data-toast')
        self.assertNotContains(response, 'js/toasts.js')
        self.assertNotContains(response, 'class="toast-region"')

    def test_delete_redirect_removes_shift_without_success_notice(self):
        start = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0)
        entry = BroadcastShift.objects.create(employee=self.user, created_by=self.user,
                                             starts_at=start, ends_at=start + timedelta(hours=1))
        response = self.client.post(reverse('newsroom:shift_delete', args=[entry.pk]), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(BroadcastShift.objects.filter(pk=entry.pk).exists())
        self.assertNotContains(response, 'Эфир удалён.')
        self.assertNoSideMessages(response)
        self.assertEqual(list(response.context['messages']), [])

    def test_profile_save_persists_without_success_notice(self):
        self.user.email = 'existing@example.org'
        self.user.save(update_fields=['email'])
        response = self.client.post(reverse('newsroom:profile'), {'first_name': 'Обновлённое имя', 'email': 'editor@example.org'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Обновлённое имя')
        self.assertEqual(self.user.email, 'existing@example.org')
        self.assertNotContains(response, 'name="email"')
        self.assertNotContains(response, 'Профиль обновлён.')
        self.assertNoSideMessages(response)
        self.assertEqual(list(response.context['messages']), [])

    def test_rejected_booking_action_shows_inline_error_once_without_changing_status(self):
        studio = Studio.objects.create(name='Студия проверки ошибок')
        start = timezone.now() + timedelta(days=1)
        booking = StudioBooking.objects.create(title='Проверка ошибки', studio=studio,
                                              starts_at=start, ends_at=start + timedelta(hours=1),
                                              responsible=self.user, created_by=self.user)
        response = self.client.post(reverse('newsroom:booking_action', args=[booking.pk]), {'action': 'status', 'status': 'unknown'}, follow=True)
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'planned')
        self.assertContains(response, 'Этот переход статуса недоступен.')
        self.assertNoSideMessages(response)
        parsed = InlineAlerts()
        parsed.feed(response.content.decode())
        self.assertEqual(len(parsed.alerts), 1)
        attributes, in_main = parsed.alerts[0]
        self.assertTrue(in_main, 'the error belongs to page content, not a floating region')
        self.assertIn('message-error', attributes['class'].split())
        self.assertNotContains(self.client.get(booking.get_absolute_url()), 'Этот переход статуса недоступен.')

    def message_request(self):
        request = RequestFactory().get('/')
        request.user = self.user
        request.session = self.client.session
        request._messages = FallbackStorage(request)
        messages.set_level(request, messages.DEBUG)
        return request

    def test_shared_renderer_escapes_errors_and_filters_success_info_and_debug(self):
        request = self.message_request()
        payload = '<img src=x onerror=alert(1)><script>alert("message-xss")</script>'
        for level in [messages.DEBUG, messages.INFO, messages.SUCCESS]:
            messages.add_message(request, level, f'hidden-level-{level}')
        for level in [messages.WARNING, messages.ERROR]:
            messages.add_message(request, level, payload, extra_tags='unexpected-tag')
        rendered = render_to_string('newsroom/partials/messages.html', {'messages': request._messages}, request=request)
        parsed = InlineAlerts()
        parsed.feed(rendered)
        self.assertEqual(len(parsed.alerts), 2)
        self.assertIn('message-warning', parsed.alerts[0][0]['class'].split())
        self.assertIn('message-error', parsed.alerts[1][0]['class'].split())
        self.assertEqual(rendered.count(str(escape(payload))), 2)
        self.assertNotIn('hidden-level-', rendered)
        self.assertNotIn('unexpected-tag', rendered)
        self.assertNotIn('<img src=x onerror=', rendered)
        self.assertNotIn('<script>alert("message-xss")</script>', rendered)
        self.assertTrue(request._messages.used)

    def test_queued_messages_of_all_levels_are_consumed_after_one_page(self):
        request = self.message_request()
        levels = [messages.DEBUG, messages.INFO, messages.SUCCESS, messages.WARNING, messages.ERROR]
        for level in levels:
            messages.add_message(request, level, f'queued-level-{level}')
        prepared = HttpResponse()
        request._messages.update(prepared)
        request.session.save()
        self.client.cookies.update(prepared.cookies)
        response = self.client.get(reverse('newsroom:profile'))
        self.assertEqual(response.status_code, 200)
        for level in [messages.DEBUG, messages.INFO, messages.SUCCESS]:
            self.assertNotContains(response, f'queued-level-{level}')
        for level in [messages.WARNING, messages.ERROR]:
            self.assertContains(response, f'queued-level-{level}')
        self.assertNoSideMessages(response)
        next_response = self.client.get(reverse('newsroom:profile'))
        for level in levels:
            self.assertNotContains(next_response, f'queued-level-{level}')
