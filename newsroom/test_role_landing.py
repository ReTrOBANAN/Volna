"""The site opens a permitted work section rather than a separate dashboard."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from .models import Department
from .permissions import assign_role
from .tests import NewsroomTestCase, TEST_PASSWORD

User = get_user_model()


class RoleLandingTests(NewsroomTestCase):
    destinations = {
        'radio_chief': 'bookings',
        'radio_reporter': 'bookings',
        'engineer': 'bookings',
        'director': 'events',
        'tv_chief': 'events',
        'tv_reporter': 'events',
        'operator': 'events',
        'photographer': 'events',
        'editor': 'events',
        'site_chief': 'events',
        'site_editor': 'events',
        'producer': 'events',
    }

    def test_root_redirects_each_role_to_an_accessible_work_section(self):
        for role, destination in self.destinations.items():
            with self.subTest(role=role):
                self.login_as(role)
                response = self.client.get(reverse('newsroom:home'), follow=True)
                self.assertRedirects(response, reverse(f'newsroom:{destination}'))
                self.assertEqual(response.resolver_match.url_name, destination)

    def test_login_without_next_opens_each_roles_default_section(self):
        for role, destination in self.destinations.items():
            with self.subTest(role=role):
                self.client.logout()
                response = self.client.post(reverse('newsroom:login'), {
                    'username': role, 'password': TEST_PASSWORD,
                }, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.resolver_match.url_name, destination)
                self.assertEqual(response.redirect_chain[-1], (reverse(f'newsroom:{destination}'), 302))

    def test_root_requires_login_and_preserves_return_address(self):
        self.assertRedirects(self.client.get(reverse('newsroom:home')), '/login/?next=/')

    def test_login_preserves_a_safe_explicit_next(self):
        target = reverse('newsroom:shifts') + '?date=2026-05-01'
        response = self.client.post(reverse('newsroom:login'), {
            'username': 'radio_reporter', 'password': TEST_PASSWORD, 'next': target,
        }, follow=True)
        self.assertRedirects(response, target)
        self.assertEqual(response.resolver_match.url_name, 'shifts')

    def test_external_next_cannot_replace_the_default_section(self):
        response = self.client.post(reverse('newsroom:login'), {
            'username': 'engineer', 'password': TEST_PASSWORD,
            'next': 'https://example.com/untrusted',
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.resolver_match.url_name, 'bookings')
        self.assertTrue(all(url.startswith('/') for url, _ in response.redirect_chain))

    def test_logged_in_login_page_uses_the_role_destination(self):
        self.login_as('editor')
        response = self.client.get(reverse('newsroom:login'), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.resolver_match.url_name, 'events')

    def test_invalid_role_configuration_is_denied_without_a_redirect_loop(self):
        missing_profile = User.objects.create_user(username='landing_no_profile')
        missing_group = self.users['radio_reporter']
        missing_group.groups.clear()
        mismatch = self.users['engineer']
        mismatch.groups.set([Group.objects.get(name='tv_reporter')])
        multiple_groups = self.users['operator']
        multiple_groups.groups.add(Group.objects.get(name='radio_reporter'))
        unknown_role = self.users['site_editor']
        unknown_role.employee.role = 'unknown_role'
        unknown_role.employee.save(update_fields=['role'])
        for user in [missing_profile, missing_group, mismatch, multiple_groups, unknown_role]:
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse('newsroom:home'), follow=True)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.redirect_chain, [])

    def test_default_destination_follows_a_role_change_in_the_same_session(self):
        user = self.users['radio_reporter']
        self.client.force_login(user)
        self.assertRedirects(self.client.get(reverse('newsroom:home')), reverse('newsroom:bookings'))
        assign_role(user, 'tv_reporter', Department.objects.get(slug='tv'))
        self.assertRedirects(self.client.get(reverse('newsroom:home')), reverse('newsroom:events'))

    def test_navigation_has_no_home_page_and_logo_keeps_the_role_entry_point(self):
        self.login_as('engineer')
        response = self.client.get(reverse('newsroom:bookings'))
        self.assertNotContains(response, '>Главная</a>')
        self.assertContains(response, 'class="brand brand-gorvolna" href="/"')
