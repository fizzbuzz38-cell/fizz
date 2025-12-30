# Generated migration to add fournisseur and nom_contact fields to Charge model

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Schoolapp', '0009_groupe_schoolversement_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='charge',
            name='fournisseur',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='charge',
            name='nom_contact',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]

