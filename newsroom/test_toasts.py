"""Server flash messages survive redirects and render as safe accessible toasts."""
from datetime import timedelta
from html.parser import HTMLParser

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from .models import BroadcastShift, Department, Studio, StudioBooking
from .permissions import assign_role, setup_roles

User = get_user_model()


class ToastMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.toasts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'data-toast' in attrs:
            self.toasts.append(attrs)


class FlashToastTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        cls.user = User.objects.create_user(username='toast_radio_chief', first_name='Главред радио')
        assign_role(cls.user, 'radio_chief', Department.objects.get(slug='radio'))

    def setUp(self):
        self.client.force_login(self.user)

    def toasts(self, response):
        parser = ToastMarkup()
        parser.feed(response.content.decode())
        return parser.toasts

    def test_delete_redirect_shows_a_success_toast_once(self):
        start = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0)
        entry = BroadcastShift.objects.create(employee=self.user, created_by=self.user, starts_at=start, ends_at=start + timedelta(hours=1))
        response = self.client.post(reverse('newsroom:shift_delete', args=[entry.pk]), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(BroadcastShift.objects.filter(pk=entry.pk).exists())
        self.assertContains(response, 'Эфир удалён.')
        toast = self.toasts(response)
        self.assertEqual(len(toast), 1)
        self.assertIn('toast-success', toast[0]['class'].split())
        self.assertEqual(toast[0]['role'], 'status')
        self.assertEqual(toast[0]['data-toast-duration'], '5000')
        self.assertContains(response, 'data-toast-close')
        self.assertNotContains(response, 'class="messages"')
        self.assertEqual(self.toasts(self.client.get(reverse('newsroom:shifts'))), [])

    def test_profile_save_uses_same_global_success_toast(self):
        response = self.client.post(reverse('newsroom:profile'), {'first_name': 'Обновлённое имя', 'email': 'editor@example.org'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Обновлённое имя')
        self.assertEqual(self.user.email, 'editor@example.org')
        self.assertContains(response, 'Профиль обновлён.')
        toast = self.toasts(response)
        self.assertEqual(len(toast), 1)
        self.assertIn('toast-success', toast[0]['class'].split())
        self.assertContains(response, 'js/toasts.js')

    def test_rejected_booking_action_shows_error_toast_without_changing_status(self):
        studio = Studio.objects.create(name='Студия уведомлений')
        start = timezone.now() + timedelta(days=1)
        booking = StudioBooking.objects.create(title='Проверка сообщения об ошибке', studio=studio,
                                              starts_at=start, ends_at=start + timedelta(hours=1),
                                              responsible=self.user, created_by=self.user)
        response = self.client.post(reverse('newsroom:booking_action', args=[booking.pk]), {'action': 'status', 'status': 'unknown'}, follow=True)
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'planned')
        toast = self.toasts(response)
        self.assertEqual(len(toast), 1)
        self.assertIn('toast-error', toast[0]['class'].split())
        self.assertEqual(toast[0]['role'], 'alert')
        self.assertEqual(toast[0]['aria-live'], 'assertive')
        self.assertEqual(toast[0]['data-toast-duration'], '8000')
        self.assertContains(response, 'Этот переход статуса недоступен.')

    def test_shared_renderer_preserves_levels_and_escapes_message_markup(self):
        request = RequestFactory().get('/')
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)
        payload = '<img src=x onerror=alert(1)><script>alert("flash-xss")</script>'
        for level in [messages.SUCCESS, messages.ERROR, messages.WARNING, messages.INFO]:
            messages.add_message(request, level, payload)
        rendered = render_to_string('newsroom/partials/toasts.html', {'messages': request._messages}, request=request)
        parser = ToastMarkup()
        parser.feed(rendered)
        self.assertEqual(len(parser.toasts), 4)
        self.assertEqual([item['data-toast-duration'] for item in parser.toasts], ['5000', '8000', '7000', '5000'])
        self.assertEqual([item['role'] for item in parser.toasts], ['status', 'alert', 'status', 'status'])
        for item, tone in zip(parser.toasts, ['success', 'error', 'warning', 'info']):
            self.assertIn(f'toast-{tone}', item['class'].split())
            self.assertEqual(item['aria-atomic'], 'true')
        self.assertEqual(rendered.count(str(escape(payload))), 4)
        self.assertNotIn('<img src=x onerror=', rendered)
        self.assertNotIn('<script>alert("flash-xss")</script>', rendered)
