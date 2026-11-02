import getpass
import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from newsroom.models import Department
from newsroom.permissions import assign_role, setup_roles


class Command(BaseCommand):
    help = 'Безопасно создать первый аккаунт директора без публичной регистрации.'

    def add_arguments(self, parser):
        parser.add_argument('--username', required=True)
        parser.add_argument('--name', default='Директор редакции')
        parser.add_argument('--email', default='')
        parser.add_argument('--generate-password', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        setup_roles()
        User = get_user_model()
        username = options['username'].strip()
        if not username:
            raise CommandError('Логин не может быть пустым.')
        if User.objects.filter(username=username).exists():
            raise CommandError('Аккаунт с таким логином уже существует; он не изменён.')
        user = User(username=username, first_name=options['name'].strip(), email=options['email'])
        if options['generate_password']:
            password = secrets.token_urlsafe(18)
        else:
            password = getpass.getpass('Пароль: ')
            if password != getpass.getpass('Повторите пароль: '):
                raise CommandError('Пароли не совпадают.')
        try:
            user.full_clean(exclude=['password'])
            validate_password(password, user)
        except ValidationError as exc:
            raise CommandError('; '.join(exc.messages)) from exc
        user.set_password(password)
        user.save()
        department, _ = Department.objects.get_or_create(slug='management', defaults={'name': 'Руководство'})
        assign_role(user, 'director', department)
        self.stdout.write(self.style.SUCCESS(f'Создан директор: {username}'))
        if options['generate_password']:
            self.stdout.write(f'Пароль (выводится один раз): {password}')
