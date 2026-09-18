from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("events", "0006_session8_sharing")]

    operations = [
        migrations.AddField(
            model_name="assetobject",
            name="content_type",
            field=models.CharField(default="image/jpeg", max_length=32),
        ),
    ]
