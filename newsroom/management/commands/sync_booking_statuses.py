import math
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, close_old_connections, connection

from newsroom.services import sync_booking_statuses


class Command(BaseCommand):
    help = 'Обновить статусы бронирований по времени; --watch продолжает проверку без открытого браузера.'

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true', help='Продолжать обновление до Ctrl+C.')
        parser.add_argument('--interval', type=float, default=1.0, help='Период проверки: от 0.1 до 3600 секунд (по умолчанию 1).')

    def handle(self, *args, **options):
        try:
            interval = float(options['interval'])
        except (TypeError, ValueError):
            raise CommandError('Интервал должен быть от 0.1 до 3600 секунд.')
        if not math.isfinite(interval) or not 0.1 <= interval <= 3600:
            raise CommandError('Интервал должен быть от 0.1 до 3600 секунд.')
        watch = options['watch']
        if watch:
            self.stdout.write(f'Автоматическое обновление статусов каждые {interval:g} с. Для остановки нажмите Ctrl+C.')
        try:
            while True:
                try:
                    count = sync_booking_statuses()
                    if count or not watch:
                        self.stdout.write(self.style.SUCCESS(f'Обновлено статусов: {count}'))
                except OperationalError as exc:
                    if not watch or connection.vendor != 'sqlite':
                        raise
                    self.stderr.write(self.style.WARNING(f'Не удалось обновить статусы в SQLite: {exc}. Повтор через {interval:g} с.'))
                finally:
                    # Standalone watch passes must release stale connections; a
                    # programmatic call must not close its caller's transaction.
                    if watch and not connection.in_atomic_block:
                        close_old_connections()
                if not watch:
                    return
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write('Автоматическое обновление статусов остановлено.')
