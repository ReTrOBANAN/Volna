from django.db import migrations
from django.db.models import F


def use_booking_creator(apps, schema_editor):
    bookings = apps.get_model('newsroom', 'StudioBooking').objects.using(schema_editor.connection.alias)
    bookings.exclude(responsible_id=F('created_by_id')).update(responsible_id=F('created_by_id'))


class Migration(migrations.Migration):
    dependencies = [('newsroom', '0007_rename_studios')]

    operations = [migrations.RunPython(use_booking_creator, migrations.RunPython.noop)]
