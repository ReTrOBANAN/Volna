from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Создать или обновить роли, подразделения и две студии.'

    def handle(self, *args, **options):
        from newsroom.permissions import setup_roles
        setup_roles()
        from newsroom.models import Studio
        for name in ["Новая студия", "Старая студия"]:
            Studio.objects.get_or_create(name=name)
        self.stdout.write(self.style.SUCCESS('Роли, подразделения и студии настроены.'))
