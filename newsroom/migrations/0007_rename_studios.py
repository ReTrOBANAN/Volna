from django.db import migrations


def rename_studios(apps, schema_editor):
    studios = apps.get_model('newsroom', 'Studio').objects.using(schema_editor.connection.alias)
    studios.filter(name='Студия №1').update(name='Новая студия')
    studios.filter(name='Студия №2').update(name='Старая студия')


def restore_studio_names(apps, schema_editor):
    studios = apps.get_model('newsroom', 'Studio').objects.using(schema_editor.connection.alias)
    studios.filter(name='Новая студия').update(name='Студия №1')
    studios.filter(name='Старая студия').update(name='Студия №2')


class Migration(migrations.Migration):
    dependencies = [('newsroom', '0006_auditentry_system_actor')]

    operations = [migrations.RunPython(rename_studios, restore_studio_names)]
